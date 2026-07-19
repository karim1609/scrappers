import os

def get_required_env(var_name: str, hint: str = "") -> str:
    """
    Fetches a required environment variable or raises a descriptive error.
    
    Args:
        var_name: The exact name of the environment variable.
        hint: Optional contextual hint (e.g., 'Get yours at developer.domain.com')
    """
    value = os.environ.get(var_name, "").strip()
    if not value:
        msg = f"{var_name} is not set. Please provide it in your environment."
        if hint:
            msg += f" {hint}"
        raise RuntimeError(msg)
    return value
