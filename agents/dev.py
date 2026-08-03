from .base_agent import BaseLocalAgent

class DevAgent(BaseLocalAgent):
    """Software Developer Agent responsible for writing Python FastAPI / Trading Bot code and fixing bugs based on feedback."""
    def __init__(self, model: str = None):
        super().__init__(
            name="Dev Agent",
            role_prompt=(
                "You are a Senior Python & Web Software Developer. Your job is to write clean, high-performance, "
                "and production-ready code (Python, FastAPI, HTML, JS) based on requirements and architectural specs. "
                "When provided with bug reports or security audit rejections, you must fix all issues and output the updated code."
            ),
            model=model
        )

    async def generate_code(self, master_plan: str, ui_spec: str = "", current_code: str = "") -> str:
        input_prompt = f"### MASTER PLAN:\n{master_plan}\n"
        if ui_spec:
            input_prompt += f"### UI SPECIFICATIONS:\n{ui_spec}\n"
        if current_code:
            input_prompt += f"### EXISTING CODEBASE:\n{current_code}\n"
        input_prompt += "\nPlease write the complete refactored/new implementation code."
        return await self.execute(input_prompt)

    async def fix_code(self, current_code: str, bug_report: str, master_plan: str) -> str:
        input_prompt = (
            f"### MASTER PLAN:\n{master_plan}\n\n"
            f"### CURRENT CODE WITH ISSUES:\n{current_code}\n\n"
            f"### REJECTION FEEDBACK / BUG REPORT:\n{bug_report}\n\n"
            "Please fix all reported bugs, vulnerabilities, and failed test cases. Return the full corrected code."
        )
        return await self.execute(input_prompt)
