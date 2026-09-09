# Freebuff Cloud — UI OCR Report (Freebuff-cloud1.png)

Source: `/home/admin/AshatHUB/Freebuff-cloud1.png` — 2777x1770 PNG (RGBA).
Tooling: rapidocr-onnxruntime 1.2.3 (ONNX det/rec), coordinates = `[x1,y1,x2,y2]`
(min/max of the 4-point det box). Rows sorted top→bottom, left→right.
Note: screenshot appears captured at ~2x scale (crisp 2777px width).

## Extracted text table

| BBox [x1,y1,x2,y2] | Text |
|---|---|
| [465,56,1276,95] | freebuff.com/cloud/project/afraid-deserts-pump |
| [2540,57,2583,91] | 15 |
| [90,174,195,210] | pi-dots |
| [467,176,711,205] | Current workspace |
| [2314,177,2438,204] | Members |
| [2536,175,2637,209] | Deploy |
| [2661,177,2718,202] | BETA |
| [19,266,409,295] | I just connected the GitHub r... |
| [2569,267,2702,291] | All threads |
| [50,343,521,369] | tent that reaches 200°F in minutes. |
| [1522,354,1626,382] | Preview |
| [1712,353,1831,386] | Changes |
| [1868,447,2392,474] | Clerk Replace hand-rolled auth with Clerk's .. |
| [16,489,124,520] | Thinking |
| [16,560,1279,589] | The tooling isn't available, so I'll proceed with the reasonable default: persist the install/ |
| [13,608,1072,643] | preview/build commands as durable scripts in the repo's (package.json |
| [1085,607,1324,640] | (the fallback the |
| [15,660,937,692] | platform guidelines endorse), verify they work, then summarize. |
| [53,732,404,760] | Write todos, Thinking, Error |
| [103,833,309,860] | This run failed |
| [104,882,1327,909] | Another instance of freebuff has taken over this session. Only one instance per account is allowed. |
| [165,950,238,985] | Retry |
| [1950,1019,2263,1047] | No dev command set |
| [1696,1067,2517,1099] | Set the command below, or ask the agent to work it out  nothing |
| [1695,1106,2518,1132] | configures it on its own. When a repo is connected, Freebuff picks |
| [114,1132,216,1160] | Arcade |
| [228,1134,263,1157] | AD |
| [1181,1142,1322,1170] | Read Now |
| [1693,1142,2521,1169] | up a dev, preview, start or serve script from package.json next to a |
| [1906,1178,2306,1203] | lockfile, if it finds one it can use. |
| [52,1193,710,1223] | Benchmark Report: Why Your MCP Tools Fail |
| [52,1241,1086,1272] | Stop paying for a bigger model to fix bad tool calls. Clear schemas took even |
| [51,1284,735,1312] | the cheapest models to 100%. See the benchmark. |
| [1980,1277,2284,1304] | Set preview command |
| [1705,1392,1819,1424] | API keys |
| [1917,1394,2028,1421] | Terminal |
| [2123,1391,2241,1422] | vS Code (≈ "VS Code") |
| [2330,1392,2548,1424] | Preview settings |
| [14,1465,75,1490] | Skills |
| [125,1464,331,1494] | Review changes |
| [395,1464,516,1491] | Run tests |
| [581,1466,768,1493] | Explain project |
| [64,1613,227,1650] | Message... |
| [637,1708,740,1736] | ¥ Skills (icon + "Skills") |
| [901,1709,1197,1735] | Report issue or feature |
| [78,1711,254,1734] | GLM 5.3 Flash |
| [348,1710,549,1737] | Reasoning Max |

## Layout inference

- **Top chrome (y 40–100):** browser-style URL bar centered ("freebuff.com/cloud/project/afraid-deserts-pump"), a "15" counter top-right (notifications/credits). The app is a desktop shell — URL bar rendered in-app, no real browser frame.
- **Header bar (y 160–215):** left shows a session/workspace id ("pi-dots") + "Current workspace" label; right side a toolbar: "Members", "Deploy" button with "BETA" badge. Classic cloud-console header.
- **Left column (x 0–1400), full height:** agent chat/thread panel. Evidence: thread message text ("I just connected the GitHub r…", "Thinking", agent narration), an error card ("This run failed" + "Another instance of freebuff has taken over this session…" + "Retry" button at y≈950), a light ad/banner card ("Arcade AD", "Read Now", "Benchmark Report: Why Your MCP Tools Fail") — light-on-dark contrast card at x≈50–1086, y≈1130–1310 — and quick-action chips ("Skills", "Review changes", "Run tests", "Explain project") above a "Message..." composer (y≈1613) with footer row: "GLM 5.3 Flash", "Reasoning Max" model pickers left, "Skills" + "Report issue or feature" right (y≈1708).
- **Right column (x ≈1500–2777):** preview/deploy workspace. Tab strip "Preview" / "Changes" (y≈354); a suggestion row ("Clerk — Replace hand-rolled auth with Clerk's…", y≈447); an empty-state card "No dev command set" with explanatory copy ("Set the command below… Freebuff picks up a dev, preview, start or serve script from package.json next to a lockfile…") and a "Set preview command" CTA (y≈1277); bottom tab strip "API keys" / "Terminal" / "VS Code" / "Preview settings" (y≈1392). Top-right also has an "All threads" chip (y≈267).
- **Split:** roughly 52/48 two-pane layout — chat/agent pane left, live preview/console pane right, separated around x≈1400–1500. Shared header on top, both panes have their own bottom action bars. No traditional left icon sidebar; navigation lives in header + tabs + chips.

## Color palette

Quantized (median-cut, top 6):

| Hex | RGB | Coverage |
|---|---|---|
| #0a0a0a | (10,10,10) | 54.6% |
| #100d0d | (16,13,13) | 22.2% |
| #292a2a | (41,42,42) | 16.4% |
| #100a0a | (16,10,10) | 5.7% |
| #080808 | (8,8,8) | 0.6% |
| #090a09 | (9,10,9) | 0.5% |

Point samples:

| Hex | Sample point | Role |
|---|---|---|
| #0a0a0a | (2000,900) main panel | near-black canvas |
| #212121 | (300,1500) chat bg, (250,970) Retry btn, (2000,1400) tab strip | elevated surface / buttons / tabs |
| #171717 | (1000,1750) composer | input bar |
| #1d1b1f | (2500,80) topbar | header chrome |
| #312f33 | (1400,60) URL bar | URL pill |
| #595959 | (700,190) workspace label | mid-gray label/segment bg |
| #ebebeb | (100,1200) banner | light banner card (only light surface on screen) |

Palette read: pure near-black (#0a0a0a) base, one gray elevation step (#212121), mid-gray accents (#595959), light gray text, single light (#ebebeb) banner card as contrast accent. Flat, minimal, no saturated brand color detected.

## UI elements worth copying

1. **Header split:** workspace id + "Current workspace" left; `Members · Deploy[BETA]` right. Beta badge as small attached pill on primary action.
2. **Two-pane agent console:** chat thread left / live preview right (~52/48), each pane owning its own action bar — the defining layout of the product.
3. **Error/run-failed card:** dark card with title ("This run failed"), full-width explanation, and a compact "Retry" button (#212121 surface) — resilient-agent UX pattern.
4. **Agent status line:** "Write todos, Thinking, Error" — inline tool/state trail in chat, chip-like tokens showing pipeline steps.
5. **Quick-action chips above composer:** Skills / Review changes / Run tests / Explain project — single-click prompts, horizontal scroll row.
6. **Composer bar:** "Message..." input + left model pickers ("GLM 5.3 Flash", "Reasoning Max") + right utilities ("Skills", "Report issue or feature"). Model selector embedded in input footer, not settings.
7. **Preview/Changes tab pair** atop right pane — code-change vs running-preview toggle.
8. **Empty-state card with CTA:** "No dev command set" + 4-line guidance + "Set preview command" button; explains auto-detection (package.json + lockfile heuristic) inside the empty state.
9. **Bottom tool tabs on right pane:** API keys / Terminal / VS Code / Preview settings — workspace-tool switcher as icon+label tab strip.
10. **Light banner ad card** (#ebebeb) with "AD" tag, headline, subcopy, "Read Now" link — deliberately inverted surface for promo content.
11. **In-app URL bar** showing project slug route (`/cloud/project/afraid-deserts-pump`) — project identity doubles as fake browser URL; slugified project names.
12. **"All threads" chip** top-right of chat pane — thread history entry point.

## Related repo (AshatHostingPlatform/apps/ashat-hub-web)

- Stack: React 19 + Vite 6 + TypeScript 5.7. Plain CSS (`src/styles.css`) — no Tailwind/Uno, no component library, no router in package.json.
- Components: `src/components/AdminSurface.tsx`, `AuthPanel.tsx`, `MemberSurfaces.tsx`, `api.ts` (+ `App.tsx`, `main.tsx`). Small surface-based structure; a Freebuff-style layout (header + two-pane console + tab strips) would be replicated with plain CSS grid/flex and hand-rolled components, matching the repo's dependency-light approach.
