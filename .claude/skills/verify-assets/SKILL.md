---
name: verify-assets
description: "Check that all referenced assets (images, sounds, fonts) are tracked in git. Use before deploying or when debugging 404s."
---

# Asset Verification

1. Find all asset references in HTML/CSS/JS files:
   - `src="..."`, `href="..."`, `url(...)`, `new Audio(...)`, `fetch(...)`
   - Filter to local paths (not http/https URLs)
2. For each referenced asset path, check if the file exists on disk
3. Check if the file is tracked by git (`git ls-files --error-unmatch <path>`)
4. Report:
   - **Missing files**: Referenced but don't exist
   - **Untracked files**: Exist but not in git (will 404 on GitHub Pages / deployment)
   - **OK**: Tracked and present
5. Offer to `git add` any untracked assets
