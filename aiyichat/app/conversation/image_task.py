"""图片生成任务的结构化描述。

独立小模块，供 engine / imagegen_service / 适配器共享，避免循环导入。
"""

from dataclasses import dataclass


@dataclass
class ImageTask:
    """单个图片生成任务。

    Attributes:
        prompt: 图片描述（英文），本人出镜时只含场景/姿态/衣着等，不含面部特征
        contains_self: 图中是否出现角色本人
        ref_image_url: 角色定妆照地址（/api/media/{id}），空则无参考图
        fallback_prompt: 降级为纯文生图时使用的完整描述（含外貌特征），
            为空时降级直接使用 prompt
        account_id: 扣了这张图额度的账号；生成或发送失败时按它退还额度
    """

    prompt: str
    contains_self: bool = False
    ref_image_url: str = ""
    fallback_prompt: str = ""
    account_id: str = ""
