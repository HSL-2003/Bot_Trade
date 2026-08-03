import json
from .base_agent import BaseLocalAgent

class QATesterAgent(BaseLocalAgent):
    """QA & Tester Agent creating pytest unit tests and validating code logic."""
    def __init__(self, model: str = None):
        super().__init__(
            name="QA Tester Agent",
            role_prompt=(
                "You are a Quality Assurance (QA) Automation Engineer. Analyze the code, generate comprehensive "
                "pytest unit tests, and check logic edge-cases. "
                "Your output MUST be a structured JSON object with keys: "
                "'all_passed' (boolean: true/false), "
                "'errors' (list of bugs or missing validation points), "
                "'unit_tests' (string containing python pytest code)."
            ),
            model=model
        )

    async def test_code(self, code: str) -> dict:
        task = f"Generate unit tests and verify the logic for the following code:\n\n{code}\n\nReturn JSON output."
        raw_res = await self.execute(task)

        try:
            json_str = raw_res
            if "```json" in raw_res:
                json_str = raw_res.split("```json")[1].split("```")[0].strip()
            elif "```" in raw_res:
                json_str = raw_res.split("```")[1].split("```")[0].strip()
            
            parsed = json.loads(json_str)
            return {
                "all_passed": bool(parsed.get("all_passed", False)),
                "errors": parsed.get("errors", []),
                "unit_tests": parsed.get("unit_tests", ""),
                "raw_output": raw_res
            }
        except Exception:
            all_passed = "ALL_TESTS_PASSED" in raw_res.upper() or "ALL_PASSED: TRUE" in raw_res.upper() or "NO_BUGS" in raw_res.upper()
            return {
                "all_passed": all_passed,
                "errors": [raw_res] if not all_passed else [],
                "unit_tests": raw_res,
                "raw_output": raw_res
            }
