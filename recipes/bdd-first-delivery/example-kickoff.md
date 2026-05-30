# Kickoff: Add user-settings page with three sections

## Goal

Add a `/settings` page to the app with three independently configurable sections:
- **Theme** — light / dark / system toggle
- **Language** — locale picker (en, de, fr, es)
- **Notifications** — email and push notification toggles

The backend settings endpoint already exists at `GET /api/user/settings` and `PATCH /api/user/settings`. You only need to build the frontend and wire it to those endpoints.

## Scope

Files you may create or modify:
- `src/pages/settings/SettingsPage.tsx` — page shell, route registration
- `src/pages/settings/ThemeSection.tsx` — theme toggle component
- `src/pages/settings/LanguageSection.tsx` — locale picker component
- `src/pages/settings/NotificationsSection.tsx` — notification toggles component
- `src/pages/settings/settingsApi.ts` — typed API client for `/api/user/settings`
- `src/App.tsx` — add route `/settings`

## Backend contract

`GET /api/user/settings` returns:
```json
{
  "theme": "light | dark | system",
  "language": "en | de | fr | es",
  "notifications": {
    "email": true,
    "push": false
  }
}
```

`PATCH /api/user/settings` accepts the same shape (partial updates allowed).

## Definition of Done

- [ ] `/settings` route renders the page for authenticated users
- [ ] ThemeSection shows the current theme and lets the user toggle it; change is persisted via PATCH
- [ ] LanguageSection shows the current locale in a dropdown; change is persisted via PATCH
- [ ] NotificationsSection shows two toggles (email, push); changes are persisted via PATCH
- [ ] Page does not crash on cold render (no provider errors, no QueryClient errors)
- [ ] All three sections load data from `GET /api/user/settings` on mount
- [ ] Error state is handled: if PATCH fails with 500, the toggle/picker reverts to the pre-change value

## Test data

Use these mock responses:
```json
{
  "theme": "dark",
  "language": "en",
  "notifications": { "email": true, "push": false }
}
```

## Notes

- Keep each section as its own independent component — they should be importable standalone.
- No inline styles. Use the project's existing design token classes.
- Authenticated route: the page is only accessible to logged-in users. Redirect to `/login` if unauthenticated.
