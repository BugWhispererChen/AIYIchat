#!/usr/bin/env python3
"""运行所有单元测试。"""

import subprocess
import sys
import os

def main():
    test_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(test_dir))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", test_dir, "-v"],
        cwd=project_root,
    )
    sys.exit(result.returncode)

if __name__ == "__main__":
    main()
