# Synthetic sample documents

The PDFs in this folder are **synthetic**: fictional, AI-generated business
documents created for this project purely as test input for the IDP pipeline.
They are organized by example industry pack:

- `construction/` — e.g. NCRs, pile/material reports, safety incident reports
- `energy/` — e.g. well completion, pipeline inspection, maintenance reports
- `healthcare/` — e.g. facility inspection, lab result, calibration reports

## Not real data

- **Invented entities and identifiers.** People, organisations, sites, assets,
  products and projects, and their addresses, postcodes, phone numbers and
  reference, licence and permit numbers, are invented. Resemblance to real
  entities is coincidental.
- **Real standards and public places.** Standards, regulations and public place
  names (cities, counties, basins, geological formations) are real, so the
  documents read realistically.
- **Marked as synthetic.** Every page carries a `SYNTHETIC DATA` banner.
- **No PII, PHI, PCI, or other regulated or confidential data.** These files do
  not contain and must not be used to represent real personal, health, or
  payment information.
- They exist only to exercise extraction, classification, spatial memory, and
  compliance logic without needing real customer documents.

If you add your own documents here, make sure they are likewise free of real or
regulated data before committing them to a public repository.

## Licence

The synthetic PDFs in this folder are dedicated to the public domain under
CC0 1.0, as their page footer states. Everything else in the repository is
licensed under MIT-0 (see [../../LICENSE](../../LICENSE)).
