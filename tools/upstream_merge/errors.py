"""上游合并工具的统一错误类型。"""


class UpstreamMergeError(ValueError):
    """输入、仓库状态或收据链无法失败关闭时抛出。"""
