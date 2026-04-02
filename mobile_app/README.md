# Study Platform Android App (React Native)

This folder contains a React Native (Expo) Android app shell for your existing Flask platform.

## Why this approach

Your current platform is server-rendered (Flask + Jinja templates + form routes, not JSON APIs), so a WebView shell gives you the same screens and behavior on Android with minimal backend changes.

## What is included

- Expo React Native app in `mobile_app/`
- Android-ready WebView screen with:
  - Cookie/session support
  - Pull to refresh
  - Hardware back button support
  - Fallback URLs for local network and production

## Configure server URL

This app now reads production URL from environment variables.

Create a local file named `.env` inside `mobile_app/`:

EXPO_PUBLIC_BASE_URL=https://your-heroku-app-name.herokuapp.com
EXPO_PUBLIC_FALLBACK_1=https://your-second-domain.com
EXPO_PUBLIC_FALLBACK_2=http://10.0.2.2:5000

Notes:
- Use your real Heroku app URL for `EXPO_PUBLIC_BASE_URL`.
- Keep `EXPO_PUBLIC_FALLBACK_2` for emulator testing if needed.

## Run locally

```bash
cd mobile_app
npm install
npm run start
```

Then press `a` in Expo CLI to open Android.

## Build APK/AAB

1. Install EAS CLI:
```bash
npm i -g eas-cli
```

2. Login and configure build:
```bash
cd mobile_app
eas login
eas build:configure
```

3. Build Android:
```bash
eas build --platform android
```

## Full online setup (GitHub + Heroku + mobile app)

1. Deploy backend from GitHub to Heroku:
- In Heroku dashboard, create/select your app.
- Connect the GitHub repository.
- Enable automatic deploy from your production branch, or click Deploy Branch manually.

2. Set Heroku config vars (minimum):
- `SECRET_KEY` with a strong random value.
- `MONGODB_URI` with your MongoDB Atlas (or production MongoDB) connection string.
- `SESSION_COOKIE_SECURE=true`.

3. Verify backend URL is healthy:
- Open `https://your-heroku-app-name.herokuapp.com` in browser.
- Confirm login page loads and static files are served.

4. Point mobile app to Heroku URL:
- Set `EXPO_PUBLIC_BASE_URL` in `mobile_app/.env`.
- Restart Expo after editing env values.

5. If building with EAS cloud, set secrets there too:

```bash
eas secret:create --scope project --name EXPO_PUBLIC_BASE_URL --value https://your-heroku-app-name.herokuapp.com
eas secret:create --scope project --name EXPO_PUBLIC_FALLBACK_1 --value https://your-second-domain.com
eas secret:create --scope project --name EXPO_PUBLIC_FALLBACK_2 --value http://10.0.2.2:5000
```

6. Build Android app:

```bash
cd mobile_app
eas build --platform android
```

7. Install and test on phone:
- Login with a real account.
- Open subjects/sections/lessons/tests.
- Confirm session stays logged in after app restart.

## Common production issues

- Login loop or session drops:
  - Ensure backend runs on HTTPS.
  - Ensure `SESSION_COOKIE_SECURE=true` in Heroku config vars.

- Heroku cold start delay:
  - First request after idle can be slow on eco dynos.

- Mixed content blocked:
  - Use HTTPS backend URL in `EXPO_PUBLIC_BASE_URL`.

## Important note

This is an exact mobile wrapper of your existing web platform UI/flows. If you want fully native screens (native navigation, native forms, native state + REST APIs), your Flask backend will need dedicated JSON API endpoints.
