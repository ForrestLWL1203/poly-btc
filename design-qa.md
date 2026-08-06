# Design QA

- Source: `/Users/forrestliao/.codex/generated_images/019fd1b2-0288-7060-858c-3ddb53e4d6e2/exec-1784c1db-76f4-4683-a2a3-7f2b2764bb2a.png`
- Implementation: `/tmp/poly-btc-design-qa-20260806/implementation-discovery-final.png`
- Desktop viewport: 1672 × 941
- Responsive viewport: 390 × 844
- State: active full scan with realistic mock progress and scan history

## Visual comparison

- Preserved the application's black page background; glass treatment is limited to content surfaces.
- Matched the selected status hierarchy, open funnel layout, restrained teal accents, and dense operational table.
- Kept all funnel values within one full-scan generation and displayed a monotonic sequence.
- Removed the final Core frame and indicator dot; its number alone remains teal and aligns with the other stages.
- Replaced the redundant history status column with leading status icons.
- Simplified scan labels to `轻量重评` and `全量重评`.
- Verified Chinese-only teal page titles for `采集` and `策略参数`.
- Verified the responsive funnel and history table at mobile width.
- Browser console had no errors or warnings.

## Intentional differences

- The implementation retains the product's existing sidebar and top control chrome, which the concept reference omitted.
- Mock values differ from the concept values, while hierarchy, relationships, and monotonic funnel behavior match.

final result: passed
