#  -*- coding: utf-8 -*-
class InvalidOrder(Exception):
    def __init__(self, message, load=None, extra=None):
        super().__init__(message)
        self.msg = message
        self.load = load
        self.extra = extra

    def __repr__(self):
        return f"{self.__class__.__name__}({self.msg!r}, load={self.load!r}, extra={self.extra!r})"


class BTXServerError(InvalidOrder):
    def __init__(self, message, load=None, extra=None):
        super().__init__(message, load, extra)


class MaxRetries(InvalidOrder):
    def __init__(self, message, load=None, extra=None):
        super().__init__(message, load, extra)


class InvalidOrdStatus(InvalidOrder):
    def __init__(self, message, load=None, extra=None):
        super().__init__(message, load, extra)


class InvalidOrderID(InvalidOrder):
    def __init__(self, message, load=None, extra=None):
        super().__init__(message, load, extra)


class InvalidOrderQty(InvalidOrder):
    def __init__(self, message, load=None, extra=None):
        super().__init__(message, load, extra)


class InsufficientBalance(InvalidOrder):
    def __init__(self, message, load=None, extra=None):
        super().__init__(message, load, extra)


class wsException(Exception):
    def __init__(self, message):
        super().__init__(message)
