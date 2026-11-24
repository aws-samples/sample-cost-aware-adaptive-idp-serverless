# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pipeline stack: Lambdas + Step Functions orchestration + EventBridge trigger.

Wires the cost-aware cascade ("AI only when needed"):

    S3 upload -> EventBridge -> Step Functions
        Preprocess (Tier 0, free: dedup + triage)
          |- duplicate? -> skip
          |- otherwise  -> Textract DetectDocumentText (basic OCR, default)
                             |- clear        -> Spatial -> Reconcile -> Sink
                             |- text unclear -> [structured Textract, opt-in]
                                                -> Nova (cheap LLM) -> learn
                                                -> Reconcile -> Sink
                             |- image/hard    -> BDA (managed) -> learn
                                                -> Reconcile -> Sink

Basic OCR + spatial memory *apply* learned layouts cheaply; the Nova and BDA
escalation tiers *train* spatial memory so repeat layouts resolve for free.
Structured Textract (FORMS/TABLES) is expensive and therefore opt-in only.

Lambdas share a pure-Python ``idp_common`` layer; some additionally use a
dependencies layer (pypdf, PyYAML, durable SDK — all pure-Python open source).
"""

import re
import sys
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
)
from aws_cdk import aws_bedrock as bedrock_l1
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from infra.config import AppConfig

_SRC = Path(__file__).resolve().parents[1] / "src"
_RUNTIME = lambda_.Runtime.PYTHON_3_14

# Whole-execution timeout. The preprocess dedup claim lease is this plus a
# 5-minute margin, so a run killed by the timeout (not seen by any Catch)
# still frees the content hash for re-upload once the lease lapses.
_STATE_MACHINE_TIMEOUT = Duration.minutes(30)
_DEDUP_LEASE_SECONDS = int(_STATE_MACHINE_TIMEOUT.to_seconds()) + 300

# Make the shared engine importable at synth time so the BDA blueprint schema
# can be derived from the active industry pack (single source of truth).
_COMMON_LAYER = _SRC / "common_layer" / "python"
if str(_COMMON_LAYER) not in sys.path:
    sys.path.insert(0, str(_COMMON_LAYER))

from idp_common import bda as bda_common  # noqa: E402
from idp_common.packs import load_pack  # noqa: E402


class PipelineStack(Stack):
    """Compute + orchestration for the born-digital processing slice."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        input_bucket: s3.Bucket,
        bda_output_bucket: s3.Bucket,
        dedup_table: dynamodb.Table,
        template_table: dynamodb.Table,
        packs_table: dynamodb.Table,
        data_key: kms.Key,
        delivery_stream_name: str,
        delivery_stream_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config
        self.packs_table = packs_table
        # Encrypts every log group this stack creates (threat T8).
        self.data_key = data_key

        # Exclude Python bytecode so the layer asset hash is deterministic.
        # Otherwise a local ``pytest`` run writes ``__pycache__/*.pyc`` into the
        # layer source, changing the asset hash, publishing a new layer version,
        # and breaking the cross-stack layer export imported by other stacks.
        _layer_exclude = ["**/__pycache__", "**/*.pyc"]
        self.common_layer = lambda_.LayerVersion(
            self,
            "CommonLayer",
            code=lambda_.Code.from_asset(
                str(_SRC / "common_layer"), exclude=_layer_exclude
            ),
            compatible_runtimes=[_RUNTIME],
            description="Shared idp_common library (models, template_memory, record).",
        )
        self.deps_layer = lambda_.LayerVersion(
            self,
            "DepsLayer",
            code=lambda_.Code.from_asset(
                str(_SRC / "deps_layer"), exclude=_layer_exclude
            ),
            compatible_runtimes=[_RUNTIME],
            description="Third-party deps (pypdf, PyYAML, durable SDK).",
        )

        # Managed BDA project (+ generic blueprint) for the visual escalation
        # tier. Created via L1 constructs — no custom resource required.
        self.bda_project = self._make_bda_project(data_key)

        self.preprocess_fn = self._make_preprocess(input_bucket, dedup_table, data_key)
        self.spatial_fn = self._make_spatial(template_table, data_key)
        self.sink_fn = self._make_sink(
            delivery_stream_name,
            delivery_stream_arn,
            input_bucket,
            dedup_table,
            data_key,
        )
        self.textract_ocr_fn, self.textract_ocr_alias = self._make_textract(
            "TextractOcrFn", "ocr", input_bucket, data_key, template_table
        )
        # Structured FORMS/TABLES Textract is the most expensive OCR mode, so it
        # is opt-in. When disabled, unclear text escalates straight to the cheap
        # LLM instead.
        if config.structured_textract_enabled:
            self.textract_forms_fn, self.textract_forms_alias = self._make_textract(
                "TextractFormsFn",
                "forms",
                input_bucket,
                data_key,
                None,
                reserved_concurrency=self._paid_tier_concurrency(),
            )
        else:
            self.textract_forms_fn, self.textract_forms_alias = None, None
        # Optional prompt-attack guardrail on the Nova fallback (threat T1).
        self.guardrail, self.guardrail_version = (
            self._make_guardrail() if config.bedrock_guardrail_enabled else (None, None)
        )
        self.fallback_fn = self._make_bedrock_fn(
            "ExtractFallbackFn",
            _SRC / "handlers" / "extract_fallback",
            input_bucket,
            data_key,
            template_table=template_table,  # Nova trains spatial from OCR layout
            reserved_concurrency=self._paid_tier_concurrency(),
            guardrail=self.guardrail,
            guardrail_version=self.guardrail_version,
        )
        self.bda_fn, self.bda_alias = self._make_bda_fn(
            input_bucket, bda_output_bucket, data_key, template_table
        )
        self.reconcile_fn = self._make_reconcile()

        self.all_functions = [
            fn
            for fn in (
                self.preprocess_fn,
                self.spatial_fn,
                self.sink_fn,
                self.textract_ocr_fn,
                self.textract_forms_fn,
                self.fallback_fn,
                self.bda_fn,
                self.reconcile_fn,
            )
            if fn is not None
        ]

        self.state_machine = self._make_state_machine()
        self._wire_eventbridge(input_bucket)
        self._create_outputs()

    # ------------------------------------------------------------------
    # Bedrock Data Automation project (managed visual extraction)
    # ------------------------------------------------------------------
    def _make_bda_project(self, data_key: kms.Key) -> bedrock_l1.CfnDataAutomationProject:
        """A BDA project with a document blueprint derived from the active pack.

        The blueprint's fields come from the active industry pack (its linking
        keys, compliance-referenced fields, and value-at-risk fields), each
        declared ``inferenceType: explicit``. BDA returns their values plus
        bounding boxes (visual grounding) in ``explainability_info``, so the
        managed visual tier extracts exactly what the reconcile/compliance layer
        expects — and those boxes train spatial memory. Switch packs with
        ``-c idp:packName=<name>`` to change the schema.
        """
        # One blueprint per document type: BDA matches each document to the
        # best-fitting blueprint (up to 40 per project) and extracts that type's
        # fields. Each blueprint's schema "class" is the document-type name, so
        # the match also tells us the doc_type.
        blueprints = self._bda_blueprints(data_key)

        # standard_output_configuration is required by the service even when
        # custom output (blueprint) is used. Enable bounding boxes so the
        # visual tier returns field positions that train spatial memory.
        Bda = bedrock_l1.CfnDataAutomationProject
        standard_output = Bda.StandardOutputConfigurationProperty(
            document=Bda.DocumentStandardOutputConfigurationProperty(
                extraction=Bda.DocumentStandardExtractionProperty(
                    bounding_box=Bda.DocumentBoundingBoxProperty(state="ENABLED"),
                    granularity=Bda.DocumentExtractionGranularityProperty(
                        types=["DOCUMENT", "PAGE", "ELEMENT"]
                    ),
                ),
                generative_field=Bda.DocumentStandardGenerativeFieldProperty(
                    state="DISABLED"
                ),
                output_format=Bda.DocumentOutputFormatProperty(
                    text_format=Bda.DocumentOutputTextFormatProperty(types=["MARKDOWN"]),
                    additional_file_format=Bda.DocumentOutputAdditionalFileFormatProperty(
                        state="DISABLED"
                    ),
                ),
            )
        )
        project = bedrock_l1.CfnDataAutomationProject(
            self,
            "BdaProject",
            project_name=f"{self.config.app_name}-visual",
            project_description="Managed visual extraction tier for cost-aware IDP.",
            kms_key_id=data_key.key_id,
            standard_output_configuration=standard_output,
            custom_output_configuration=bedrock_l1.CfnDataAutomationProject.CustomOutputConfigurationProperty(
                blueprints=[
                    bedrock_l1.CfnDataAutomationProject.BlueprintItemProperty(
                        blueprint_arn=bp.attr_blueprint_arn,
                    )
                    for bp in blueprints
                ]
            ),
        )
        for bp in blueprints:
            project.add_dependency(bp)
        project.apply_removal_policy(self.config.removal_policy)
        return project

    def _bda_blueprints(self, data_key: kms.Key) -> list:
        """Build one BDA document blueprint per document type in the pack.

        Each blueprint's fields come from that document type (``fields_for``),
        each declared ``inferenceType: explicit``, and its schema ``class`` is
        the document-type name. Falls back to a single generic blueprint if the
        pack can't be loaded, so synth never hard-fails.
        """
        base = f"{self.config.app_name} {self.config.pack_name}"
        # (type_name, fields, field_descriptions)
        specs = []
        try:
            pack = load_pack(self.config.pack_name)
            for dt in pack.document_types:
                specs.append(
                    (dt.name, pack.fields_for(dt.name), pack.descriptions_for(dt.name))
                )
        except Exception:  # noqa: BLE001 - degrade to a single generic blueprint
            specs = [
                (
                    "document",
                    ["document_title", "document_date", "reference_number", "total_amount"],
                    {},
                )
            ]

        blueprints = []
        for type_name, fields, field_descriptions in specs:
            safe = re.sub(r"[^A-Za-z0-9-]", "-", type_name)
            cid = "BdaBlueprint" + "".join(p.capitalize() for p in type_name.split("_"))
            schema = bda_common.build_blueprint_schema(
                fields,
                class_name=type_name,
                description=f"{base} {type_name} fields.",
                field_descriptions=field_descriptions,
            )
            bp = bedrock_l1.CfnBlueprint(
                self,
                cid,
                blueprint_name=f"{self.config.app_name}-{self.config.pack_name}-{safe}-doc",
                type="DOCUMENT",
                # Keep the serialized schema ASCII — BDA validates it against a
                # restricted character set.
                schema=schema,
                kms_key_id=data_key.key_id,
            )
            bp.apply_removal_policy(self.config.removal_policy)
            blueprints.append(bp)
        return blueprints

    # ------------------------------------------------------------------
    # Lambda functions
    # ------------------------------------------------------------------
    def _log_group(self, name: str) -> logs.LogGroup:
        """Explicit log group so retention is configurable (Lambda default is
        never-expire, which silently accrues CloudWatch Logs storage cost).
        Encrypted with the solution's customer-managed key (threat T8)."""
        return logs.LogGroup(
            self,
            name,
            retention=self.config.log_retention,
            removal_policy=self.config.removal_policy,
            encryption_key=self.data_key,
        )

    def _paid_tier_concurrency(self):
        """Reserved concurrency for paid-tier Lambdas (threat T5), or ``None``
        when ``idp:paidTierMaxConcurrency`` is 0 (cap disabled)."""
        return self.config.paid_tier_max_concurrency or None

    def _make_preprocess(
        self,
        input_bucket: s3.Bucket,
        dedup_table: dynamodb.Table,
        data_key: kms.Key,
    ) -> lambda_.Function:
        fn = lambda_.Function(
            self,
            "PreprocessFn",
            runtime=_RUNTIME,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(_SRC / "handlers" / "preprocess")),
            layers=[self.common_layer, self.deps_layer],
            timeout=Duration.seconds(120),
            memory_size=1024,
            environment={
                "DEDUP_TABLE": dedup_table.table_name,
                # Size/page limits checked before any paid tier (threat T5).
                "MAX_FILE_BYTES": str(self.config.max_file_size_mb * 1024 * 1024),
                "MAX_PAGES": str(self.config.max_pages),
                # In-progress dedup claim outlives the whole execution, so a
                # timed-out run (which no Catch sees) frees the hash (threat T4).
                "DEDUP_LEASE_SECONDS": str(_DEDUP_LEASE_SECONDS),
            },
            log_group=self._log_group("PreprocessLogs"),
        )
        input_bucket.grant_read(fn)
        dedup_table.grant_read_write_data(fn)
        data_key.grant_encrypt_decrypt(fn)
        return fn

    def _make_spatial(
        self, template_table: dynamodb.Table, data_key: kms.Key
    ) -> lambda_.Function:
        fn = lambda_.Function(
            self,
            "SpatialFn",
            runtime=_RUNTIME,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(_SRC / "handlers" / "spatial")),
            layers=[self.common_layer],
            timeout=Duration.seconds(60),
            memory_size=512,
            environment={
                "TEMPLATE_TABLE": template_table.table_name,
                "SIMILARITY_THRESHOLD": "0.7",
            },
            log_group=self._log_group("SpatialLogs"),
        )
        template_table.grant_read_write_data(fn)
        data_key.grant_encrypt_decrypt(fn)
        return fn

    def _make_sink(
        self,
        delivery_stream_name: str,
        delivery_stream_arn: str,
        input_bucket: s3.Bucket,
        dedup_table: dynamodb.Table,
        data_key: kms.Key,
    ) -> lambda_.Function:
        fn = lambda_.Function(
            self,
            "SinkFn",
            runtime=_RUNTIME,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(_SRC / "handlers" / "sink")),
            layers=[self.common_layer],
            timeout=Duration.seconds(60),
            memory_size=256,
            environment={
                "DELIVERY_STREAM_NAME": delivery_stream_name,
                "ANNOTATIONS_ENABLED": str(self.config.annotations_enabled).lower(),
                # Sink marks the dedup claim DONE once the record is delivered,
                # so only completed documents count as duplicates (threat T4).
                "DEDUP_TABLE": dedup_table.table_name,
            },
            log_group=self._log_group("SinkLogs"),
        )
        dedup_table.grant_write_data(fn)
        data_key.grant_encrypt_decrypt(fn)
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["firehose:PutRecord", "firehose:PutRecordBatch"],
                resources=[delivery_stream_arn],
            )
        )
        # Optional S3 Annotations backend (object tagging); harmless when off.
        if self.config.annotations_enabled:
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["s3:PutObjectTagging"],
                    resources=[input_bucket.arn_for_objects("*")],
                )
            )
        return fn

    def _make_textract(
        self,
        construct_id: str,
        tier: str,
        input_bucket: s3.Bucket,
        data_key: kms.Key,
        template_table,
        reserved_concurrency=None,
    ):
        """Durable Textract function (one handler, ``ocr``/``forms`` via env).

        Returns ``(function, alias)``. Durable functions must be invoked by a
        qualified ARN, so Step Functions targets the alias. The durable handler
        uses ``wait`` between async status checks (suspends without compute
        charge) and reports back to Step Functions via the task token.
        """
        environment = {"TIER": tier}
        if template_table is not None:
            environment["TEMPLATE_TABLE"] = template_table.table_name
        if tier == "ocr":
            # Share of trusted-template documents re-checked by AI (threat T2).
            environment["SPOT_CHECK_PERCENT"] = str(self.config.spot_check_percent)
        fn = lambda_.Function(
            self,
            construct_id,
            runtime=_RUNTIME,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(_SRC / "handlers" / "textract")),
            layers=[self.common_layer, self.deps_layer],  # deps has the durable SDK
            timeout=Duration.seconds(300),
            memory_size=1024,
            environment=environment,
            log_group=self._log_group(f"{construct_id}Logs"),
            reserved_concurrent_executions=reserved_concurrency,
            durable_config=lambda_.DurableConfig(
                execution_timeout=Duration.hours(1),
                retention_period=Duration.days(7),
            ),
        )
        # Checkpoint permissions for durable execution.
        fn.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSLambdaBasicDurableExecutionRolePolicy"
            )
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "textract:DetectDocumentText",
                    "textract:AnalyzeDocument",
                    "textract:StartDocumentTextDetection",
                    "textract:GetDocumentTextDetection",
                    "textract:StartDocumentAnalysis",
                    "textract:GetDocumentAnalysis",
                ],
                resources=["*"],  # Textract actions are not resource-scoped
            )
        )
        # Report completion/failure back to the Step Functions task token.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["states:SendTaskSuccess", "states:SendTaskFailure"],
                resources=["*"],  # token-scoped; not resource-addressable in IAM
            )
        )
        input_bucket.grant_read(fn)
        data_key.grant_decrypt(fn)
        if template_table is not None:
            template_table.grant_read_data(fn)

        alias = lambda_.Alias(
            self,
            f"{construct_id}Alias",
            alias_name="live",
            version=fn.current_version,
        )
        return fn, alias

    def _make_bedrock_fn(
        self,
        construct_id: str,
        asset_dir,
        input_bucket: s3.Bucket,
        data_key: kms.Key,
        template_table=None,
        reserved_concurrency=None,
        guardrail=None,
        guardrail_version=None,
    ) -> lambda_.Function:
        """Zip Lambda that calls Bedrock (Converse) for text/vision extraction.

        When ``template_table`` is supplied, the function also trains spatial
        memory from the upstream OCR layout, so grant it read/write. When a
        ``guardrail`` is supplied, the function screens the OCR text with it via
        ApplyGuardrail (scoped to that guardrail's ARN only).
        """
        environment = {
            "BEDROCK_MODEL_ID": self.config.bedrock_model_id,
            # The LLM fallback classifies + extracts using the active pack's
            # document types and field names, so compliance/linking match. The
            # pack is read from the packs table at runtime (no redeploy to change
            # fields/doc types), with the bundled seed as fallback.
            "PACK_NAME": self.config.pack_name,
            "PACKS_TABLE": self.packs_table.table_name,
        }
        if template_table is not None:
            environment["TEMPLATE_TABLE"] = template_table.table_name
            # Learned templates stay provisional until confirmed (threat T2).
            environment["TEMPLATE_CONFIRMATIONS"] = str(
                self.config.template_confirmations
            )
        if guardrail is not None:
            environment["GUARDRAIL_ID"] = guardrail.attr_guardrail_id
            environment["GUARDRAIL_VERSION"] = guardrail_version.attr_version
        fn = lambda_.Function(
            self,
            construct_id,
            runtime=_RUNTIME,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(asset_dir)),
            # deps_layer supplies PyYAML, needed by the pack loader's fallback
            # to the bundled YAML when the packs table has no matching item.
            layers=[self.common_layer, self.deps_layer],
            timeout=Duration.seconds(120),
            memory_size=1024,
            environment=environment,
            log_group=self._log_group(f"{construct_id}Logs"),
            reserved_concurrent_executions=reserved_concurrency,
        )
        # Scope InvokeModel to the specific model this deployment is configured
        # to use (idp:bedrockModelId), not all Bedrock models. A cross-region
        # inference-profile id (geo prefix such as "us.") also needs the
        # underlying foundation model in each Region the profile can route to;
        # the member Regions below are the US set that the default
        # us.* profiles use — adjust them for a non-US profile.
        model_id = self.config.bedrock_model_id
        base_model_id = re.sub(r"^(us-gov|us|eu|apac)\.", "", model_id)
        if base_model_id != model_id:
            cris_regions = ("us-east-1", "us-east-2", "us-west-1", "us-west-2")
            model_resources = [
                f"arn:{self.partition}:bedrock:{self.region}:{self.account}"
                f":inference-profile/{model_id}",
            ] + [
                f"arn:{self.partition}:bedrock:{region}::foundation-model/{base_model_id}"
                for region in cris_regions
            ]
        else:
            model_resources = [
                f"arn:{self.partition}:bedrock:{self.region}::foundation-model/{model_id}",
            ]
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=model_resources,
            )
        )
        if guardrail is not None:
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock:ApplyGuardrail"],
                    resources=[guardrail.attr_guardrail_arn],
                )
            )
        input_bucket.grant_read(fn)
        data_key.grant_encrypt_decrypt(fn)
        self.packs_table.grant_read_data(fn)
        if template_table is not None:
            template_table.grant_read_write_data(fn)
        return fn

    def _make_guardrail(self):
        """Bedrock Guardrail with a prompt-attack filter (threat T1, opt-in).

        The Nova fallback screens the document's OCR text with
        ``ApplyGuardrail`` before calling the model, so its own instruction and
        system prompt are never screened. The prompt-attack filter applies to
        input text only (output strength NONE), so PDF/image bytes sent to the
        model are not screened.
        """
        guardrail = bedrock_l1.CfnGuardrail(
            self,
            "PromptAttackGuardrail",
            name=f"{self.config.app_name}-prompt-attack",
            description="Blocks prompt-injection attempts in document text sent to the LLM fallback.",
            blocked_input_messaging="Blocked: the document text looks like a prompt attack.",
            blocked_outputs_messaging="Blocked by the prompt-attack guardrail.",
            content_policy_config=bedrock_l1.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock_l1.CfnGuardrail.ContentFilterConfigProperty(
                        type="PROMPT_ATTACK",
                        input_strength="HIGH",
                        output_strength="NONE",
                    )
                ]
            ),
        )
        # A guardrail version is an immutable snapshot taken once, when this
        # resource is created. Later edits to the filter configuration above
        # update only the guardrail's DRAFT; Nova keeps using this version
        # until the resource is replaced (for example, change its logical ID
        # or description so CloudFormation publishes a new version).
        version = bedrock_l1.CfnGuardrailVersion(
            self,
            "PromptAttackGuardrailVersion",
            guardrail_identifier=guardrail.attr_guardrail_id,
        )
        return guardrail, version

    def _make_bda_fn(
        self,
        input_bucket: s3.Bucket,
        bda_output_bucket: s3.Bucket,
        data_key: kms.Key,
        template_table: dynamodb.Table,
    ):
        """Durable BDA function (async invoke -> wait -> read output -> learn).

        Returns ``(function, alias)``. Like Textract async, it runs as a durable
        function invoked with the Step Functions task-token pattern; it suspends
        without compute charge during the BDA wait.
        """
        fn = lambda_.Function(
            self,
            "BdaFn",
            runtime=_RUNTIME,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(_SRC / "handlers" / "bda")),
            layers=[self.common_layer, self.deps_layer],  # deps has durable SDK
            timeout=Duration.seconds(300),
            memory_size=1024,
            environment={
                "BDA_PROJECT_ARN": self.bda_project.attr_project_arn,
                "BDA_OUTPUT_BUCKET": bda_output_bucket.bucket_name,
                "TEMPLATE_TABLE": template_table.table_name,
                "ACCOUNT_ID": self.account,
                # Map the matched blueprint back to a pack document type, and
                # learn templates tagged with their doc type.
                "PACK_NAME": self.config.pack_name,
                "PACKS_TABLE": self.packs_table.table_name,
                # Learned templates stay provisional until confirmed (T2).
                "TEMPLATE_CONFIRMATIONS": str(self.config.template_confirmations),
            },
            log_group=self._log_group("BdaFnLogs"),
            # Caps concurrent paid BDA work (threat T5); throttled async
            # invokes are retried by Lambda's async event queue.
            reserved_concurrent_executions=self._paid_tier_concurrency(),
            durable_config=lambda_.DurableConfig(
                execution_timeout=Duration.hours(1),
                retention_period=Duration.days(7),
            ),
        )
        fn.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSLambdaBasicDurableExecutionRolePolicy"
            )
        )
        # InvokeDataAutomationAsync is resource-scopable (AwsSolutions-IAM5):
        # grant it on this project plus the US cross-region data-automation
        # inference profiles it runs through (matches the AWS BDA cross-Region
        # policy example). These profile ARNs are US-only; adjust the region
        # list if you deploy the sample outside the US partitions.
        bda_profile_arns = [
            f"arn:{self.partition}:bedrock:{region}:{self.account}"
            ":data-automation-profile/us.data-automation-v1"
            for region in ("us-east-1", "us-east-2", "us-west-1", "us-west-2")
        ]
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeDataAutomationAsync"],
                resources=[self.bda_project.attr_project_arn, *bda_profile_arns],
            )
        )
        # GetDataAutomationStatus operates on a runtime invocation ARN, which is
        # not resource-addressable in IAM, so it requires "*".
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:GetDataAutomationStatus"],
                resources=["*"],
            )
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["states:SendTaskSuccess", "states:SendTaskFailure"],
                resources=["*"],  # token-scoped; not resource-addressable in IAM
            )
        )
        input_bucket.grant_read(fn)
        bda_output_bucket.grant_read_write(fn)
        data_key.grant_encrypt_decrypt(fn)
        template_table.grant_read_write_data(fn)
        self.packs_table.grant_read_data(fn)

        alias = lambda_.Alias(
            self,
            "BdaFnAlias",
            alias_name="live",
            version=fn.current_version,
        )
        return fn, alias

    def _make_reconcile(self) -> lambda_.Function:
        """Pack-driven linking + compliance. Uses PyYAML from the deps layer.

        Reads the active pack from the packs table at runtime (falling back to
        the bundled seed), so compliance/linking field changes need no redeploy.
        """
        fn = lambda_.Function(
            self,
            "ReconcileFn",
            runtime=_RUNTIME,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(_SRC / "handlers" / "reconcile")),
            layers=[self.common_layer, self.deps_layer],
            timeout=Duration.seconds(60),
            memory_size=256,
            environment={
                "PACK_NAME": self.config.pack_name,
                "PACKS_TABLE": self.packs_table.table_name,
            },
            log_group=self._log_group("ReconcileLogs"),
        )
        self.packs_table.grant_read_data(fn)
        return fn

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    def _make_state_machine(self) -> sfn.StateMachine:
        preprocess = tasks.LambdaInvoke(
            self,
            "Preprocess",
            lambda_function=self.preprocess_fn,
            payload_response_only=True,
            retry_on_service_exceptions=True,
        )
        spatial = tasks.LambdaInvoke(
            self,
            "Spatial",
            lambda_function=self.spatial_fn,
            payload_response_only=True,
            retry_on_service_exceptions=True,
        )
        sink = tasks.LambdaInvoke(
            self,
            "Sink",
            lambda_function=self.sink_fn,
            payload_response_only=True,
            retry_on_service_exceptions=True,
        )
        # Durable Textract functions are invoked via the task-token pattern:
        # the function suspends without compute charge during the async wait and
        # calls SendTaskSuccess with the result when the job completes.
        def _textract_payload(
            spot_check: bool = False, include_document: bool = False
        ) -> sfn.TaskInput:
            payload = {
                "bucket": sfn.JsonPath.string_at("$.bucket"),
                "key": sfn.JsonPath.string_at("$.key"),
                "doc_id": sfn.JsonPath.string_at("$.doc_id"),
                "source_uri": sfn.JsonPath.string_at("$.source_uri"),
                "content_hash": sfn.JsonPath.string_at("$.content_hash"),
                # Carry is_image through so the post-OCR NeedsVision? choice
                # (which routes on $.is_image) still sees it: the task-token
                # result replaces the state data, so anything the downstream
                # choices read must be echoed back in the handler output.
                "is_image": sfn.JsonPath.string_at("$.is_image"),
                "taskToken": sfn.JsonPath.task_token,
            }
            if spot_check:
                # Template spot-check (threat T2): the OCR tier's flag and the
                # trusted template id must reach the AI tiers (only the OCR
                # output carries these keys, so the first OCR call omits them).
                payload["spot_check"] = sfn.JsonPath.string_at("$.spot_check")
                payload["spot_check_template_id"] = sfn.JsonPath.string_at(
                    "$.spot_check_template_id"
                )
            if include_document:
                # BDA compares its result with the OCR document on a spot-check.
                payload["document"] = sfn.JsonPath.object_at("$.document")
            return sfn.TaskInput.from_object(payload)

        textract_ocr = tasks.LambdaInvoke(
            self,
            "TextractCheapOCR",
            lambda_function=self.textract_ocr_alias,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            # Durable functions with an execution timeout over 15 minutes cannot
            # be invoked synchronously; invoke asynchronously and let the handler
            # report back via the task token when the async job completes.
            invocation_type=tasks.LambdaInvocationType.EVENT,
            payload=_textract_payload(),
            retry_on_service_exceptions=True,
        )
        # Managed visual escalation (BDA) uses the same durable task-token shape.
        bda = tasks.LambdaInvoke(
            self,
            "BedrockDataAutomation",
            lambda_function=self.bda_alias,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            invocation_type=tasks.LambdaInvocationType.EVENT,
            payload=_textract_payload(spot_check=True, include_document=True),
            retry_on_service_exceptions=True,
        )
        bedrock_fallback = tasks.LambdaInvoke(
            self,
            "NovaFallback",
            lambda_function=self.fallback_fn,
            payload_response_only=True,
            retry_on_service_exceptions=True,
        )
        reconcile = tasks.LambdaInvoke(
            self,
            "Reconcile",
            lambda_function=self.reconcile_fn,
            payload_response_only=True,
            retry_on_service_exceptions=True,
        )

        # Paid-tier concurrency cap (threat T5): the Nova Lambda is invoked
        # synchronously, so a throttle surfaces here. CDK's
        # retry_on_service_exceptions does not cover throttles; retry them.
        bedrock_fallback.add_retry(
            errors=["Lambda.TooManyRequestsException"],
            interval=Duration.seconds(5),
            max_attempts=8,
            backoff_rate=2,
        )

        # Failure path (threat T4): any error after preprocess releases the
        # in-progress dedup claim so the same file can be re-uploaded, then
        # fails the execution so WorkflowFailuresAlarm still fires.
        processing_failed = sfn.Fail(
            self,
            "ProcessingFailed",
            error="IdpProcessingFailed",
            cause=(
                "Extraction failed after preprocess; dedup claim released so the "
                "document can be re-uploaded."
            ),
        )
        release_claim = tasks.LambdaInvoke(
            self,
            "ReleaseDedupClaim",
            lambda_function=self.preprocess_fn,
            payload=sfn.TaskInput.from_object(
                {
                    "action": "release_dedup",
                    "content_hash": sfn.JsonPath.string_at("$.content_hash"),
                    "doc_id": sfn.JsonPath.string_at("$.doc_id"),
                }
            ),
            payload_response_only=True,
            result_path="$.release",
            retry_on_service_exceptions=True,
        )
        release_claim.add_catch(
            processing_failed, errors=["States.ALL"], result_path="$.releaseError"
        )
        release_claim.next(processing_failed)
        post_preprocess_tasks = [
            textract_ocr,
            bda,
            bedrock_fallback,
            spatial,
            reconcile,
            sink,
        ]

        # Shared tail: every extraction path -> reconcile -> sink -> done.
        done = sfn.Succeed(self, "Done")
        reconcile.next(sink).next(done)
        spatial.next(reconcile)
        bedrock_fallback.next(reconcile)  # LLM output flows through reconcile too
        bda.next(reconcile)  # BDA output flows through reconcile too

        # Text-unclear escalation: cheap LLM by default. When structured
        # Textract is opted in, try FORMS/TABLES first and fall back to the LLM
        # only if it still recovered nothing.
        if self.textract_forms_alias is not None:
            textract_forms = tasks.LambdaInvoke(
                self,
                "TextractStructured",
                lambda_function=self.textract_forms_alias,
                integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
                invocation_type=tasks.LambdaInvocationType.EVENT,
                payload=_textract_payload(spot_check=True),
                retry_on_service_exceptions=True,
            )
            post_preprocess_tasks.append(textract_forms)
            textract_forms.next(
                sfn.Choice(self, "FormsClear?")
                .when(sfn.Condition.boolean_equals("$.clear", True), spatial)
                .otherwise(bedrock_fallback)
            )
            text_escalation = textract_forms
        else:
            text_escalation = bedrock_fallback

        # OCR unclear: route images/charts/visually-hard docs to BDA, and
        # text-heavy docs to the (opt-in structured Textract ->) LLM path.
        vision_choice = (
            sfn.Choice(self, "NeedsVision?")
            .when(sfn.Condition.boolean_equals("$.is_image", True), bda)
            .otherwise(text_escalation)
        )

        # Tier 1 cheap OCR: accept when clear, otherwise escalate.
        ocr_clarity_choice = (
            sfn.Choice(self, "OcrClear?")
            .when(sfn.Condition.boolean_equals("$.clear", True), spatial)
            .otherwise(vision_choice)
        )
        textract_ocr.next(ocr_clarity_choice)

        # The error output is stored under $.error so the original state
        # (doc_id, content_hash) is still there for ReleaseDedupClaim.
        for task in post_preprocess_tasks:
            task.add_catch(release_claim, errors=["States.ALL"], result_path="$.error")

        duplicate_skipped = sfn.Succeed(self, "DuplicateSkipped")
        # Over the size/page limits (threat T5): stop before any paid tier.
        rejected_over_limit = sfn.Succeed(self, "RejectedOverLimit")
        definition = preprocess.next(
            sfn.Choice(self, "Duplicate?")
            .when(
                sfn.Condition.boolean_equals("$.duplicate", True),
                duplicate_skipped,
            )
            .when(
                sfn.Condition.boolean_equals("$.rejected", True),
                rejected_over_limit,
            )
            .otherwise(textract_ocr)
        )

        return sfn.StateMachine(
            self,
            "ProcessingStateMachine",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            state_machine_type=sfn.StateMachineType.STANDARD,
            timeout=_STATE_MACHINE_TIMEOUT,
            tracing_enabled=True,
            logs=sfn.LogOptions(
                destination=logs.LogGroup(
                    self,
                    "StateMachineLogs",
                    retention=self.config.log_retention,
                    removal_policy=self.config.removal_policy,
                    encryption_key=self.data_key,
                ),
                # Log every state transition, not just errors (AwsSolutions-SF1),
                # for full execution auditability. Execution data is NOT logged
                # (threat T8): state input/output carries extracted field values
                # (personal or health data for some packs) and task tokens, so
                # only state names, types and timings are written. Inspect a run's
                # data in the Step Functions console instead.
                level=sfn.LogLevel.ALL,
                include_execution_data=False,
            ),
        )

    def _wire_eventbridge(self, input_bucket: s3.Bucket) -> None:
        """Start the state machine on every new object in the input bucket."""
        rule = events.Rule(
            self,
            "ObjectCreatedRule",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={"bucket": {"name": [input_bucket.bucket_name]}},
            ),
        )
        rule.add_target(
            targets.SfnStateMachine(
                self.state_machine,
                input=events.RuleTargetInput.from_object(
                    {
                        "bucket": events.EventField.from_path("$.detail.bucket.name"),
                        "key": events.EventField.from_path("$.detail.object.key"),
                    }
                ),
            )
        )

    def _create_outputs(self) -> None:
        CfnOutput(
            self,
            "StateMachineArn",
            value=self.state_machine.state_machine_arn,
            description="Step Functions state machine orchestrating the pipeline.",
        )
