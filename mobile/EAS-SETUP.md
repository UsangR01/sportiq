# EAS build setup

Everything that can be prepared without an Expo account is done: `eas.json` holds the three
build profiles, and `app.json` now carries the iOS bundle identifier and Android package a real
build requires.

The remaining steps need **your Expo credentials**, so they cannot be run unattended.

## 1. Create the project — DONE

```
Project:    @usangr01/sportiq
projectId:  b7215410-13a9-4825-8602-c601e27bdace
Dashboard:  https://expo.dev/accounts/usangr01/projects/sportiq
```

`app.json` now carries `extra.eas.projectId` and `owner: usangr01`, both written by `eas init`.

**Run every `eas` command from `mobile/`, not the repo root.** `app.json` lives here, and from
the root the CLI reports only "Run this command inside a project directory."

The project was created under the personal account rather than `usangr01s-team` (Expo
auto-creates that organisation alongside a personal account). Projects can be transferred from
the dashboard if it should live under the org instead.

## 2. Push notifications are now unblocked

`getExpoPushTokenAsync` needs a real `projectId`, and `lib/notifications.ts:109` reads exactly
the key `eas init` wrote. Until this existed no push token could be minted at all, which is why
push had never been verified end to end despite the backend side (`PUT /user/push-token`,
`notify_users._send_push`) being real and tested.

Still unverified **on a device** — that needs a development build (next section), because Expo
Go on Android cannot receive remote push at all.

There is a second, separate reason on Android: **Expo Go dropped remote push in SDK 53**.
`lib/notifications.ts` detects this (`isExpoGoOnAndroid`) and no-ops rather than crashing. A
`development` build is the only way to exercise push on a real Android device.

```bash
npx eas-cli build --profile development --platform android
```

## 3. Point the build at a reachable API

`eas.json`'s `development` profile ships `EXPO_PUBLIC_API_URL=http://localhost:8000`, which is
correct for an emulator (the client rewrites `localhost` to `10.0.2.2` on Android) but **wrong
for a real device** — a phone's `localhost` is the phone. Set the dev machine's LAN IP:

```json
"env": { "EXPO_PUBLIC_API_URL": "http://192.168.x.x:8000" }
```

`preview` and `production` point at `https://sportiq-api.onrender.com`, which assumes the
service name in `infra/render.yaml`. Update both if it is deployed elsewhere.

Also add the deployed origin to the backend's `CORS_ORIGINS` — native builds are not subject to
CORS, but the web target is.

## 4. Decision you can still change — but not for long

`com.sportiq.app` is used for both the iOS bundle identifier and the Android package, replacing
Expo's scaffold default `com.anonymous.sportiq`.

**An Android package name is permanent after the first Play Store release.** Changing it later
means a new listing and losing existing installs. It costs nothing to change right now, so
decide before submitting — particularly if you own a different domain worth using in reverse-DNS
form.

## 5. Not set up

- `owner` is absent from `app.json`. Add your Expo username/organisation if the project should
  belong to one rather than your personal account.
- `EXPO_ACCESS_TOKEN` is still unprovisioned on the backend. Expo accepts unauthenticated
  sends at a lower rate limit, so pushes work without it — worth adding before real volume.
- No store credentials, no `submit` configuration beyond an empty production profile.
