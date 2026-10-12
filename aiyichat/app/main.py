"""应用入口薄壳：真实逻辑位于编译后的 app.bootstrap 模块。"""
from app.bootstrap import create_app

app = create_app()
