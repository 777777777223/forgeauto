[app]
# 应用信息
title = 自动挂机合成
package.name = forgeauto
package.domain = org.forgeauto

source.dir = .
source.include_exts = py,json
source.include_patterns = assets/**,*.png,*.json
version = 0.1

requirements = python3,kivy,pyjnius==1.8.0

orientation = portrait
fullscreen = 0

android.permissions = INTERNET
android.archs = arm64-v8a

# 使用打过补丁的本地 p4a 源码（workflow 中创建，修复 pip 26 拒绝 android wheel）
p4a.source_dir = /tmp/p4a_patched

[buildozer]
log_level = 2
warn_on_root = 1
