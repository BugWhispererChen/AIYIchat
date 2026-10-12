# 预置角色定妆照资源说明

- **素材来源**：本目录 10 张定妆照均由项目内置的 ImageGen 服务（阿里云 Wanx 文生图）生成，
  生成 prompt 见 `tools/seed_companion_profiles.py` 中各角色的 `appearance` 字段。
- **权属说明**：均为 AI 生成的虚构人物形象，非真人肖像，不含第三方受版权保护的素材。
  若你所在司法辖区对 AI 生成图像另有标注或登记要求，请由部署方自行履行。
- **入库方式**：执行 seed 时按内容 sha256 幂等导入 MongoDB `media` 集合，
  并写入对应角色的 `reference_image_url`；重复执行不会产生重复记录，运行期不依赖外网。
- **替换方式**：可在管理后台「角色管理」中为角色重新上传定妆照；
  或替换本目录同名文件后重新执行 `python -m tools.seed_companion_profiles --force`。
  用户已在后台编辑过的角色（`updated_at > created_at`）默认跳过覆盖，仅补齐缺失的定妆照。
