# Craftly Studio — seller UI design

Eight designed mobile screens, editable HTML/CSS/JavaScript source, individual 2× PNG exports, two overview boards, and extra interaction-state previews.

## Open the design

Open `index.html` in Chrome, Edge or Safari. At desktop size, the left-hand screen navigator lets you jump to any page. On a phone, the design fills the viewport. The prototype works without installation or a build step.

Main screens: Onboarding, Home, New Listing, Confirm & Publish, Pending, My Listings, Orders and Settings. Bottom navigation appears only on Home, Listings, Orders, Settings and Pending.

The initial prototype contains clearly illustrative product listings, pending captures and sample orders so that all screens are reviewable. Onboarding appears until Continue is pressed. Reset local data clears the seller's local listings, pending captures and preferences. Orders remain a sample scenario.

## Interactions

- Choose English or Hindi. Telugu, Tamil and Kannada selection keeps English interface copy, as requested.
- Set the optional AI order-call consent toggle (off by default).
- Take/pick an image and record audio. Browser permission is required for a real microphone; a secure local server/HTTPS is recommended. Sample photo and voice controls make the design easy to try without permissions.
- Create is disabled until photo and voice are present. Review the draft, play/pause the readback, expand Edit details, then publish.
- Offline captures remain on this device. Switching Simulate offline off retries the queue; ready captures still require confirmation.
- Sample orders progress New → Accepted → Packed → Dispatched. Packed orders show an illustrative packing slip and barcode.
- Language, consent, local listings, queue and sample order progress persist in browser storage. This is a design prototype, not a production data store.

## Optional capture-service connection

No backend service is bundled or running by default. The default is an interactive local design preview; creating and publishing in this mode does not contact a production service.

To connect your existing service, serve these files and add `?api=http://localhost:8000` (or your actual API origin) to the URL. Configure CORS on the service for the UI origin if they differ. A sample voice cannot be sent to the live API: record actual audio first.

- `POST /listing/create`: multipart image, audio, artisan_id, language_hint.
- `POST /listing/confirm`: listing_id, confirmed and **only changed** fields in corrections.

The preview uses the clearly identified `design-preview-artisan` ID; replace it with the authenticated artisan ID when integrating. Backend status is marked connected only after a successful service response. API contract handling was verified with mocked responses; no live service was contacted.

## Visual direction

Warm ivory `#FAF7F0`, deep teal `#1C554B`, terracotta `#B56142`, sage and restrained amber. Georgia editorial headings paired with familiar sans-serif labels. Primary actions have generous touch areas; imagery celebrates hands, materials and the work itself. Original AI-generated editorial imagery is illustrative, not a claim about a real artisan or product.

## Files

- `index.html`, `style.css`, `app.js`: editable prototype source.
- `assets/`: original artwork used in the design.
- `screens/01-…08-…png`: all eight primary screens, 860 × 1864 pixels each.
- `Craftly_Screens_1.png`, `Craftly_Screens_2.png`: overview boards.
- `states/`: expanded review, offline, recording, empty and Hindi states.
- `ASSET_PROMPTS.md`: original image prompts and generation provenance.

## Validation

Checked all eight routes at a 430 × 932 phone viewport, plus horizontal overflow at 360 pixels. Browser interaction checks passed for onboarding, tab visibility, photo/audio gating, review/publish, persistence, offline queue and retry, order progression, Hindi selection and other-language fallback. API mocks verified multipart creation and changes-only confirmation. All eight main screenshots were visually inspected.
