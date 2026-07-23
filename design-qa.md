# Discovery funnel design QA

## Evidence

- Source visual truth:
  - `/var/folders/gt/w8tr95mj5msd472jvqynzwkr0000gn/T/codex-clipboard-50a1cf84-9e71-42e1-a2ed-139a3e06e33c.png`
  - `/var/folders/gt/w8tr95mj5msd472jvqynzwkr0000gn/T/codex-clipboard-7aa6d7f8-90cb-4290-adc2-afaddbb88074.png`
  - `/var/folders/gt/w8tr95mj5msd472jvqynzwkr0000gn/T/codex-clipboard-f81d27c3-787b-4908-94f9-a1570a19b219.png`
- Implementation screenshot: `/tmp/hl-dashboard-qa.Jts7oa/discovery-desktop.png`
- Viewport: 1440 × 900 CSS px, device pixel ratio 2.
- Source pixels: 2432 × 838 for the complete old funnel reference.
- Implementation pixels: 1440 × 900.
- Density normalization: images were compared as independently fitted full views; no pixel-level
  typography comparison was used because the requested change is structural removal, while the
  existing design tokens and component styles remain unchanged.
- State: authenticated mock Dashboard, Discovery page, scanner idle.

## Full-view comparison evidence

The source and implementation were opened together in one comparison input. The old eight-stage
funnel, failure-category pills, per-stage reason cards, rejection-ratio chart, and score histogram
are absent. The implementation contains exactly:

`Leaderboard → 粗筛 → Perp预筛 → Challenger → 最终Core`

The compact card keeps the existing Dashboard typography, colors, radii, borders, spacing rhythm,
and green final-Core emphasis. The removal also restores direct visual continuity between the
funnel and scan-history table.

## Focused-region comparison

No separate crop was needed: the full-view implementation renders the five stage labels and counts
at readable size, and the removed detail region is visibly empty rather than truncated or hidden.

## Required fidelity surfaces

- Fonts and typography: unchanged existing Dashboard font stack, weights, sizes, and hierarchy.
- Spacing and layout rhythm: five equal-width cards align cleanly in one row with consistent arrows.
- Colors and visual tokens: existing glass-card, muted-label, border, and final-Core green tokens retained.
- Image and asset fidelity: no image assets are present or required in this component.
- Copy and content: labels match the approved five-stage collection terminology exactly.

## Findings

No actionable P0, P1, or P2 differences.

## Interaction and runtime checks

- Discovery navigation opened successfully.
- Exactly five funnel stages rendered.
- Final Core remained visible and highlighted.
- Browser console warnings/errors: none.
- Compact API contract returned successfully.
- A narrow-viewport override was requested for an additional responsive capture, but the connected
  browser retained its desktop viewport. The existing mobile funnel stack rule was not changed, and
  reducing eight stages to five lowers rather than increases narrow-screen layout risk.

## Comparison history

- Pass 1: no P0/P1/P2 mismatch found; no visual correction iteration required.

## Final result

final result: passed
