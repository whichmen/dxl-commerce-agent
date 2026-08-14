# QianNiu Accessibility Worker (Event-driven)

This is a native Android worker for QianNiu customer service.
It replaces Appium polling with an `AccessibilityService` event-driven flow.

## Why this project

- No Appium server.
- No instrumentation/socket-hang-up loop.
- No OCR dependency.
- Directly listens to UI accessibility events and only handles new customer messages.

## Runtime flow

1. Accessibility event arrives from `com.taobao.qianniu`.
2. Worker parses list/chat UI tree.
3. If list has pending conversation (`xx秒/xx分钟` first), click into chat.
4. In chat: extract latest customer-side messages after the last agent-side message.
5. POST `/v1/decide` to the Decision API, which uses OpenClaw + the configured LLM in Agent mode.
6. Fill input box + click send.
7. POST `/v1/worker/ack`.
8. Back to list and wait for next event.

## Project path

- Service: `app/src/main/java/com/dxl/kefu/qianniu/QianNiuAccessibilityService.kt`
- Config UI: `app/src/main/java/com/dxl/kefu/qianniu/MainActivity.kt`
- HTTP client: `app/src/main/java/com/dxl/kefu/qianniu/DecisionApiClient.kt`

## Build

Open this folder in Android Studio and run:

- Build APK: `app` module
- Install to device via Android Studio or `adb install -r`

> Note: current Ubuntu host has no `java/gradle`, so build should be done in Android Studio.

## Configure on phone

1. Launch app `QianNiu Worker`.
2. Save config:
   - decision URL, tenant/store info.
3. Open accessibility settings and enable this service.
4. Open QianNiu app.

## ADB helpers

```bash
# check status
./scripts/adb_check_accessibility.sh <udid>

# install debug apk and enable service
./scripts/adb_install_and_enable.sh <udid> app/build/outputs/apk/debug/app-debug.apk

# enable service via adb
./scripts/adb_enable_service.sh <udid>

# watch worker logs
./scripts/adb_logcat_worker.sh <udid>
```

## Migration from old Appium worker

- Disable old `qianniu_mobile_worker.py` in `deploy/workers.watchdog.conf`.
- Keep other platform workers unchanged.
- Run this Android service on each phone independently.
