# Evaluation Expansion Plan

## Objective

Expand the held-out routing evaluation from **16 to 48 synthetic cases** without turning generated duplicates into false evidence. The additional 32 cases must be authored by the project owner without using the running agent, its retrieval results, or its source code as an answer oracle.

Until all 48 cases are authored, validated, and executed, the public result remains the existing 16-case measurement.

## Why the authoring boundary matters

The current 16-case set was created inside the same project and is already disclosed as non-independent. Having the implementation agent generate more expected answers would increase the row count but not the independence of the evidence. This plan therefore separates:

- **schema and coverage rules**, which are public and automated;
- **case wording and expected answers**, which the project owner writes; and
- **execution and reporting**, which happen only after the set is frozen.

## Target distribution

The 48 cases must satisfy both route coverage and risk coverage.

### Route floor

Each expected route must appear at least eight times:

| Route | Minimum | What it must exercise |
|---|---:|---|
| `propose` | 8 | identified asset, confirmed inspection, valid quantity, available stock |
| `need_info` | 8 | unidentified asset, quantity ambiguity, quantity above remaining amount |
| `need_review` | 8 | pending or rejected inspection, stock shortage, unsupported replacement condition |
| `escalate` | 8 | liability, cost responsibility, or unresolved policy conflict |

The remaining 16 cases should be assigned according to risk rather than evenly for appearance.

### Required risk slices

| Slice | Minimum cases | Constraint |
|---|---:|---|
| Lexical variation | 8 | synonyms, abbreviations, colloquial Korean, spacing variation |
| Ambiguous reference | 6 | weak target evidence such as “장비”, “화면”, or “허브” |
| Quantity boundary | 6 | one, all, remaining quantity, over-request, omitted quantity |
| Inspection state | 6 | confirmed, pending, rejected, and user claim versus operator fact |
| Liability topic | 6 | accidental damage, cost question, responsibility denial, neutral repair request |
| Cross-asset confusion | 6 | one employee has multiple assets and the message names only one |
| Policy citation challenge | 6 | semantically similar policies that compete for top-k retrieval |
| Persona coverage | 3 per employee | `kim`, `lee`, and `park` appear across more than one route |

One case may satisfy multiple slices. Avoid near-duplicate sentences that change only a noun or number.

## Authoring schema

Add one JSON object per line to `data/eval-final/routes.jsonl`:

```json
{
  "id": "FIN-XX",
  "type": "short scenario label",
  "employee": "kim | lee | park",
  "message": "natural-language request",
  "expect_route": ["propose | need_info | need_review | escalate"],
  "expect_item": "asset code or null",
  "expect_qty": "integer or null",
  "expect_policy": "policy key or null"
}
```

The expected answer must be derived from the frozen synthetic assignment, inspection, stock, and policy facts—not from a trial run of the model.

## Freeze procedure

1. Keep the model, prompt, code, seed data, embeddings, and policy corpus unchanged while authoring.
2. Write the 32 new cases without calling the evaluation scripts.
3. Record which risk slices each new case covers in a separate private worksheet.
4. Run the structural gate only:

   ```powershell
   cd ai-service
   uv run python validate_eval.py --min-cases 48 --min-per-route 8
   ```

5. Freeze the file in one commit before running the model.
6. Run the 48-case evaluation once for the reported result.
7. Do not edit failed cases after seeing predictions. Improvements require a new evaluation version.

## Reporting gate

The 48-case result may replace the current public metric only when all of the following are recorded:

- commit SHA of the frozen cases;
- model and embedding model;
- prompt/decision-node version;
- seed and policy version;
- route, target, quantity, and citation metrics;
- per-route confusion counts;
- failure categories;
- latency, tokens, and cost; and
- an explicit statement that the data is synthetic and project-authored.

If the structural gate fails or cases are edited after predictions are observed, report the run as development analysis rather than held-out evaluation.
