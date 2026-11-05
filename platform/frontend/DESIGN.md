# Oneport platform UI design guide

Reference: Datadog DRUIDS (semantic tokens, telephone test) and UniFi (quiet surfaces, one accent, status as dot plus text). Datadog and UniFi are structure references only; the palette below is this app's own.


## Datadog design-system analysis (2026-11-05, source of truth for page structure)

Sources: DRUIDS (druids.datadoghq.com) principles, foundations (color, typography, spacing, elevation), patterns (quiet states, dataviz); official product screens: Cloud Security Summary, Compliance Frameworks, Monitors list, APM service page, Synthetics test, Deployment Tracking, Cloudcraft. Colors are NOT taken from Datadog; only structure, hierarchy and behavior.

### What earlier rounds got wrong
We copied component shapes (cards, pills, tables) without Datadog's page grammar, so pages read as a generic admin template: stacked boxes of equal weight, sections with a title only, numbers floating without units, decoration standing in for meaning.

### Page grammar
1. Product header: product title (Title 26px/600, letter-spacing -0.5px) with the product's sub-views as top tabs (Monitors: List · Triggered · Downtimes) or a left sub-nav (Cloud Security: Summary · Findings · Compliance). Our products: 마이그레이션 (개요 · 작업 상세), 환경, 보안 (개요 · 문제 · 표준).
2. Scope filter row directly under the header: "이 페이지 필터" + labeled dropdowns (환경 [전체], 작업 [전체]). Every widget below respects it.
3. Sections are collapsible and always carry three things: title (Title 18px/600) + one gray sentence that says what the section answers ("인프라 전체에서 보안 기준을 얼마나 지키는지 보여 줍니다") + one right-aligned action that leads deeper ("문제 목록 보기 →"). A section without a question and a next step is not a section.
4. Summary cards (CSM Coverage cards): 3 per row max, equal height. Anatomy: title in 13px/600 uppercase-style label at top-left; a short status label at top-right only when it carries judgement ("높은 준수", "낮은 준수"); the key value large (Title 26–32px) followed by its unit in regular weight ("57점", "90% coverage"); one supporting line with per-environment or per-provider values, each prefixed by the provider logo. No icons for decoration, no tinted card backgrounds.
5. One protagonist visualization per section that tells the section's story (CSM uses a funnel; APM uses requests/errors bars). Titles above charts ≥ 13px with an inline gray summary.
6. Lists (Monitors, Findings): left facet panel with collapsible groups (group header = gray fill row with chevron), each option = checkbox + 3px vertical status bar in the status color + label + right-aligned count; above the table "N개 중 1–50" and quick filters; table headers 12px/600 uppercase-style; status column as a compact label (contrast text on soft fill, fixed width, centered). Row click opens detail (inline expansion in this app).
7. Quiet states: healthy is quiet; empty states say what would appear and what to do.
8. Typography scale (DRUIDS basic text): 11/16, 12/20, 13/20 (default), 15/24, 18/28, 23/36. Titles: 18/24, 22/28, 26/36, 32/38. Weights 400/600 only.
9. Spacing: 8px scale with 2/4 at the small end; section padding 16–24; card padding 16–20; gap between cards 16.
10. Elevation: page sections are flat (1px border, white on light-gray page); shadows only for overlays (menus, popovers, the canvas resource card).

### Copy rules (DRUIDS Plain Language + telephone test)
- Natural sentences, not fragments. Units always attached to numbers.
- Section description = the question the section answers, one sentence.
- Action labels are verbs + destination ("문제 목록 보기", "환경에서 보기").

## Tokens (source of truth: `src/styles/tokens.css`)

| Role | Token | Value |
|---|---|---|
| Canvas | `--surface-canvas` | #F7F7F7 |
| Surface | `--surface` | #FFFFFF |
| Subtle / sunken fill | `--surface-subtle` / `--surface-sunken` | #F2F2F2 / #E8E8E8 |
| Border | `--border` / `--border-strong` | #E5E5E5 / #CFCFCF |
| Text | `--text` / `--text-secondary` / `--text-tertiary` | #171717 / #525252 / #8A8A8A |
| Accent (only one) | `--accent` / hover / contrast / soft / ring | #0F5C63 / #0B4A50 / #0B4A50 / #E6F0F0 / rgba(15,92,99,.35) |
| Success | base / contrast / soft | #2F7D32 / #24622A / #E8F3E8 |
| Warning | base / contrast / soft | #B26B00 / #8A5300 / #FBF1DF |
| Danger | base / contrast / soft | #C2362B / #A02A20 / #FBE9E7 |
| Other (unknown, neutral) | base / contrast / soft | #8A8A8A / #525252 / #F2F2F2 |
| Severity | critical / high / medium / low | #C2362B / #D9661F / #B8860B / #8A8A8A |
| Charts | source (legacy) / target (new) / queue | graphite #6B6B6B / petrol #0F5C63 / light petrol #6FA3A7 |

Neutrals are pure gray. No warm tones, no purple, no blue. Old variable names (`--ink`, `--sub`, `--card`, `--ok`, `--bad` ...) are aliases of the semantic tokens above.

Canvas-only colors live in `features/environments/blockStyle.ts` and `IsoCanvas.tsx` (SVG needs hex): block tops are petrol and gray family, containers follow the AWS group convention desaturated.

## Status usage

- Status colors only convey status. Text uses the `*-contrast` token, fills use `*-soft`.
- Normal is quiet: a small green dot plus plain text, or no marker at all.
- A problem gets a compact label (contrast text on soft fill, no border, 12px, 4px radius). One label per row at most.
- Unknown is neutral gray text, never red or amber. Say why when we know ("상태 미수집", "미확인").
- Counts live inside the sentence, not in `[N 실패]` pills.

## Badges, tags, chips

- No filled labels. Status is a 8px dot plus text in the status contrast color. `StatusDot` and `StateLabel` are the only status components; a problem differs from normal only by dot and text color.
- Severity appears as a small colored dot plus text (`SeverityDot`), or as group headers in lists. There is no severity pill.
- No decorative chips: standards, resources and environment states are plain text; links use accent contrast text.
- Segmented controls and tabs mark the selected item with accent soft fill and accent text.

## Surfaces

- No tinted card backgrounds. Cards are white with a 1px border, or have no border inside a section. Overview bands use vertical 1px dividers.
- No colored score boxes and no split (green/red/gray) bars. A score is a number; a share is one neutral 4px track with one petrol fill.
- Remediation and notes are plain text with a 2px left rule, not a tinted box.

| Before | After |
|---|---|
| Pink-tinted environment cards with `[10 실패]` pill | Three columns divided by 1px lines: score `57 /100`, one line "실패 10 · 미확인 3 · 통과 10" (only 실패 in danger text), one 4px track |
| `71점` in an amber box plus red/green/gray split bar | `71` bold with gray "8개 중 5개 통과" and plain colored text "높음 2" |
| Facet sidebar plus table with severity pills | One toolbar row, inbox list grouped by severity headers, dot + text per environment |
| Tinted remediation box | "조치 방법" paragraph with a 2px left rule |

## Type, numbers, spacing

- Body 14px, table headers and labels 12px, graph titles 15px. Weights 400 and 600 only.
- Numbers: tabular figures, right aligned in tables, units always attached (`30초`, `1,200건`, `15ms`). Big numbers are not used; the key figure of a page is written into one sentence (job headline).
- 8px spacing scale. Cards: 4 to 6px radius, no shadow. Only the resource card, tooltips and popovers float.

## Copy rules (telephone test: say what a coworker would say)

- Sentences end with 합니다 or 입니다. Labels and table headers are nouns.
- Always give a noun to a ratio: "구성 요소 16개 중 15개 정상", not "15/16".
- Avoid bare internal ids and English-Korean mixes when a Korean word exists. IDs go in gray next to the title.
- Say what to do next, not only what is wrong.

| Bad | Good |
|---|---|
| [문제 없음] 구성 요소 정상 15/16 · 확인 불가 1 | 정상 운영 중 / 구성 요소 16개 중 15개 정상, 1개는 상태 미수집 |
| [10 실패] 점수 57 · 확인 불가 3 | 점검 23개 중 10개 실패, 3개 미확인 · 보안 점수 57 |
| Synced · Healthy | 정상 |
| RUNNING · 태스크 1/1 | 실행 중 · 작업 1개 중 1개 실행 |

## Icons

- Product marks: simple-icons 16.36.0 (CC0), copied as path data in `brandIcons.ts`.
- AWS: official AWS Architecture Icons package 07312026 from aws.amazon.com/architecture/icons, files in `src/assets/cloud-icons/aws/`. Used under the AWS trademark and icon guidelines; do not recolor or alter.
- Google Cloud: official core-products and category icon packages from cloud.google.com/icons, files in `src/assets/cloud-icons/gcp/`. Used under the Google Cloud brand guidelines; do not recolor or alter.
- Official icons keep their own brand colors; they are the only non-token colors in `src/assets`.
- Canvas cubes: top face is the service color, left face 0.82 and right face 0.68 of the same color. AWS services use the official category colors (RDS #C925D1, MSK and ELB #8C4FFF, S3 #7AA116, CloudWatch #E7157B, EKS #ED7100) with the white official glyph on the top face; these are the only brand colors outside the token set. Group icons (region, VPC, subnet) are drawn small at the start of the container label.

## Overview page skeleton (UniFi)

- Overview pages (#environments, #security) have no stacked section boxes. Layout is a 264px sticky left rail plus a main area; the main area's protagonist (architecture canvas, findings table) fills its full width.
- Rail: 13px key-value rows (label secondary, value tertiary), 11px 600 labels, checkbox plus name plus tertiary count, thin dividers, no cards, no big numbers.
- Tables: 28px rows, 11px 600 headers, no row separators, hover fill only, status as dot, unknown rows in tertiary text, expansion is an indented key-value block.
- Tabs on these pages are small text segments (selected = accent soft fill). Type sizes on these pages: 11, 13, 15, 19 only (canvas excepted).
- Severity is a 3-cell mini bar (critical in danger color, the rest neutral).

| Before | After |
|---|---|
| Chevron section boxes with 18px titles, summary-card rows | Left rail with key-value rows and one divider, main area without boxes |
| Facet sidebar inside a box, inbox list with group headers | Filter rail, one dense table with severity mini bars and environment dots |
| Grouped component tables per environment | One list with an environment column, segment and search |

## Copy sweep rules (round 11)

- A section description is optional. When present it is a short factual scope line, a noun phrase of about 25 Korean characters or fewer. It never describes the UI or how to read it.
- Sub-block captions (a chart or list title inside a section) get no description.
- Empty states are one short sentence naming what is missing.
- No "한눈에", "~을 보여 줍니다", "같은 눈금으로 비교합니다", "행을 누르면 ~ 펼쳐집니다".

| Bad | Good |
|---|---|
| 환경이 보안 기준을 얼마나 지키는지 한눈에 보여 줍니다 | 환경별 보안 기준 준수율 |
| 환경마다 점검한 통제가 통과, 실패, 미확인으로 얼마나 나뉘는지 같은 눈금으로 비교합니다 | (no description) |
| 표시할 단계 기록이 아직 없습니다. 작업이 시작되면 단계별 진행이 시간 순으로 나타납니다 | 단계 기록이 아직 없습니다. |

## Tone (vault korean-reply-tone, round 12)

- Sentence-type copy (headlines, status sentences, empty states, notices, error text, popovers) uses 해요체: "~해요", "~예요", "~했어요", "~없어요". No "~습니다" or "~니다". Result first, short, one idea per sentence.
- Instructions the user must act on may use "~하세요" (remediation, required permission). Nothing else gets an imperative.
- Labels, titles, table headers, section scope lines and chips stay noun phrases with no ending.
- No AI words (성공적으로, 완벽히, 원활하게, 한눈에). No dashes or arrows inside sentences. Data notation such as "온프렘 k3s → AWS EKS" in a path column is fine.
- Backend strings (control why, issue titles, events, component details) already follow this tone and are not changed in the frontend.

| Before | After |
|---|---|
| 쓰기 2,978건이 AWS EKS에 반영됐습니다. 실패한 쓰기 0건, 쓰기 중단은 최대 30초, 데이터 불일치 0건입니다. | 쓰기 2,978건이 AWS EKS로 넘어갔어요. 실패한 쓰기는 없고, 쓰기 중단은 최대 30초였어요. 데이터 불일치도 없어요. |
| 도착 환경에서 약해진 통제가 없습니다 | 도착 환경에서 새로 실패한 통제는 없어요 |
| 단계 기록이 아직 없습니다. | 단계 기록이 아직 없어요. |
| 변경분 동기화가 끝났습니다. 승인하면 쓰기 차단부터 시작되고, 전환 단계부터는 되돌릴 수 없습니다. | 변경분 동기화가 끝났어요. 승인하면 쓰기 차단부터 시작해요. 전환 단계부터는 되돌릴 수 없어요. |
| 시계열을 불러오지 못했습니다. 자동으로 다시 시도합니다. | 시계열을 불러오지 못했어요. 자동으로 다시 시도해요. |
