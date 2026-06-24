class ColesCliError(Exception):
    """Base exception for expected CLI failures."""


class AuthenticationError(ColesCliError):
    """Coles did not reach an authenticated account page."""


class InteractiveAuthenticationRequired(AuthenticationError):
    """Coles requires a human login in the opened browser."""


class ElementNotFoundError(ColesCliError):
    """A required Coles UI element was not visible."""


class ColesUnavailableError(ColesCliError):
    """Coles showed an unavailable or blocking state."""


class ProductNotFoundError(ColesCliError):
    """The requested product result could not be found."""


class OrderNotFoundError(ColesCliError):
    """The requested order could not be opened or found."""


class CartError(ColesCliError):
    """The trolley/cart could not be read or changed."""


class CheckoutError(ColesCliError):
    """Checkout could not be completed."""
