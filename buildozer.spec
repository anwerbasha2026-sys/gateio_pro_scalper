[app]
title = Gateio Spot Pro Scalper
package.name = gateiospotproscalper
package.domain = org.gateiospotproscalper
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json
source.exclude_dirs = tests,bin,.buildozer,.github,__pycache__
version = 1.0.0

# Pure-Python dependencies only for fast, reliable Android APK builds
requirements = python3,kivy==2.3.0,requests,urllib3,certifi,idna,charset-normalizer

orientation = portrait
fullscreen = 0

android.permissions = INTERNET,ACCESS_NETWORK_STATE,WAKE_LOCK
android.api = 33
android.minapi = 24
android.ndk = 28c
android.accept_sdk_license = True
android.archs = armeabi-v7a
android.allow_backup = True
p4a.branch = master

[buildozer]
log_level = 2
warn_on_root = 1
