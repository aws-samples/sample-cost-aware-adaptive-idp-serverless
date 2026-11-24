# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared, pure-Python building blocks for the cost-aware adaptive IDP pipeline.

This package is deployed as a Lambda layer and imported by every handler. It
has no third-party dependencies so it can be unit-tested without an AWS
account and without native build steps.
"""

__version__ = "0.1.0"
