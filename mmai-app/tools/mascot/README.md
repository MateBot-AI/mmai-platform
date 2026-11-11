# MateBot mascot tooling (5 November 2026, register I20 / P5)

Both scripts are offline build tools; nothing here is served by the app. They read the approved brand
sources in `../../../brand/` and write the web assets in `client/img/`. No paid generation is used.

| Script | Source | Output |
| --- | --- | --- |
| `cutout_master.py` | `brand/approved/MateBot_and_HomeBot_Approved_Master_20261104.png` (MateBot crop) | `client/img/matebot_landing.webp`, `matebot_main.webp`, `matebot_main.png` (931x791 RGBA cut-out, used on the landing and done screens) |
| `patch_poses.py [pose ...]` | `brand/poses/matebot_pose_<pose>.png` (29 poses, already white/plum/orange) + the master's "S" glyph and red hollow locator pin | `client/img/matebot_<pose>.webp` + `.png` (360x540), contact sheet `/tmp/matebot_poses_sheet.png`, log `/tmp/matebot_poses_log.json` |

Run from the `mmai-app` directory: `python3 tools/mascot/cutout_master.py`, `python3 tools/mascot/patch_poses.py` (about 70 s for all 29).

## What `patch_poses.py` changes in each pose (and nothing else)
* Tummy "AI" -> "SI": the A is erased to body colour and replaced by the master's S glyph (recoloured orange,
  slightly thickened to match the pose stroke weight, rotated to the pose's own tilt); the pose's own I is
  kept and re-spaced. Only white body pixels are painted, so hands, props and the broom handle stay in front.
* Round red ball -> red hollow locator pin from the master, scaled to the ball width x0.82 and attached to the
  top of the existing dark spring (tip overlaps the spring by 4 px). Ball glow / motion ghosts are removed
  (the running pose has an extra clean-up window above the helmet).
* `key` pose: the yellow key is removed and the raised fist filled (P5, no key prop). The `house` pose keeps
  its small house prop (it is a question illustration, not a HomeBot/house identity element).
* The `star` pose uses a slightly smaller pin because the star sits directly behind the antenna.

## Limitations
* The S is a re-coloured copy of the master glyph, not hand-lettered per pose; its weight is matched by dilation.
* Poses are otherwise the original 29 illustrations (I20(c) distinct poses preserved); pose-specific props such
  as pillow / clipboard / broom partly cover the "SI" exactly as they covered the old "AI".
* Superseded "AI"/ball/key/house artwork (`matebot_main.jpg`, `matebot_main_sm.webp`) was moved out of the
  served folder to `brand/legacy_old_ai_artwork_20261105/`.
