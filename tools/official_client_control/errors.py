"""FW-D 通用控制面的错误类型。"""


class ControlError(ValueError):
    """输入、状态或不可变性违反控制面合同。"""
