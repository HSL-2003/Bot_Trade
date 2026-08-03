from .base_agent import BaseLocalAgent

class UIDesignerAgent(BaseLocalAgent):
    """UI/UX Designer Agent responsible for UI Specs, CSS Tokens, and Layout Design."""
    def __init__(self, model: str = None):
        super().__init__(
            name="UI/UX Designer Agent",
            role_prompt=(
                "You are an expert UI/UX Designer & Frontend Architect. Your job is to design modern, "
                "accessible, glassmorphism/dark mode web interface layouts, CSS tokens, and component structure. "
                "Provide clear UI specifications and styled HTML/CSS design tokens."
            ),
            model=model
        )
