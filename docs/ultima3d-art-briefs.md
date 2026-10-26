# Ultima3D Art Briefs — AGP Studios Cinematic Redesign

Status: **BLOCKED on artwork production.** Layout and frontend work is done;
the items below need a Blender 5.2 + Ultima3D MCP session, which cannot run
from the web workspace. The site currently ships a clearly marked CSS
composition (`.hero-art` + `.hero-art-badge`) as the stand-in.

## Constraints (from the Ultima3D tree, v0.4)

- Builders are **stylized low-poly procedural** (`tree`, `rock`, `wall`,
  `column`, `stairs`, `fence`, `arch`, `roof`, ...). A photorealistic misty
  vista is out of reach; aim for a deliberate stylized interpretation with
  EEVEE atmosphere (fog, depth, warm practical lights).
- `set_material` exposes color / roughness / metallic only — confirm whether
  emission is available for torch flames; otherwise fake glow with bright
  albedo and let the director judge the render.
- Renders come from `render_asset` (8 EEVEE views) for the refine loop; final
  delivery is a baked still, not a GLB.

## Asset 1 — Homepage hero vista (HIGH priority)

- **Mood:** the approved reference (misty pine path, dim overcast sky, warm
  torchlight accents). Restraint over spectacle: no dragons, castles-as-hero,
  or saturated purple/blue.
- **Subject:** an ancient stone waymarker arch with a winding path leading
  past pines toward a hazy ridgeline. Must NOT depict Paws & Parcels or
  Everenfell specifically.
- **Composition (non-negotiable):** 2560×1440, headline occupies the lower
  third — keep the lower-center/left dark and quiet for
  "Worlds Beyond / Imagination." plus the standfirst. Subject interest sits
  upper-right of center.
- **Palette:** slate base `#101417`, cool grey-greens, torch amber keyed to
  `#B79A65`. No neon, no heavy gradients in the render itself.
- **Delivery:** `hero-vista-2560.png` master + `hero-vista-2560.avif` /
  `.webp` web copies; mobile portrait crop `hero-vista-portrait.*`
  (1152×2048 source). Place in `apps/ashat-hub-web/public/`.

## Asset 2 — Studio/about artwork (MEDIUM, deferred)

No About section ships in the current scope (per owner decision to keep the
page minimal). When one is approved: a quiet Ultima3D interior/exterior
(stone + timber + warm practicals), 1920×1080 source, same palette rules.

## Featured game artwork (no Ultima3D work)

Paws & Parcels and Everenfell keep their authentic posters. Do not generate
replacement key art. If a third title needs a panel before real art exists,
use the typography-first fallback frame, not a generated stand-in.

## Integration plan (for the frontend pass after delivery)

1. Drop exports into `apps/ashat-hub-web/public/`.
2. In `styles.css`, layer the image over the gradient stack:
   `.hero-art { background-image: url('/hero-vista-2560.avif'); ... }`
   keeping the gradients + vignette underneath as the loading fallback.
3. Serve the portrait crop under 760px via a media query.
4. Preload the desktop vista (`<link rel="preload" as="image">` in
   `index.html`); keep `loading="lazy"` off the hero — it is the LCP.
5. Remove `.hero-art-badge` once the real vista ships.
6. Respect `prefers-reduced-motion`: the vista is a static image, so no
   motion handling is needed beyond what already ships.
