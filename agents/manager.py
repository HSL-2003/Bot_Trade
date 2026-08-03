import logging
from typing import Dict, Any
from .base_agent import BaseLocalAgent
from .designer import UIDesignerAgent
from .dev import DevAgent
from .security import AppSecAgent
from .qa import QATesterAgent
from .config import MAX_FEEDBACK_RETRIES

logger = logging.getLogger(__name__)

class ManagerAgent(BaseLocalAgent):
    """
    Project Manager & Lead Architect Agent (Orchestrator).
    Manages SDLC stages and runs the Iterative Self-Correction Feedback Loop.
    """
    def __init__(self, model: str = None):
        super().__init__(
            name="Manager & Architect Agent",
            role_prompt=(
                "You are the Lead Project Manager & System Architect. Your job is to analyze user requests, "
                "create a structured Master Implementation Plan, assign tasks to worker agents, evaluate feedback, "
                "and approve final product releases."
            ),
            model=model
        )
        self.designer = UIDesignerAgent(model=model)
        self.dev = DevAgent(model=model)
        self.security = AppSecAgent(model=model)
        self.qa = QATesterAgent(model=model)

    async def create_master_plan(self, user_request: str, current_code: str = "") -> str:
        prompt = (
            f"Analyze user request and create a detailed step-by-step Master Implementation Plan:\n"
            f"User Request: {user_request}\n"
        )
        if current_code:
            prompt += f"Current Codebase:\n{current_code}\n"
        return await self.execute(prompt)

    async def run_sdlc_loop(self, user_request: str, current_code: str = "", max_retries: int = MAX_FEEDBACK_RETRIES) -> Dict[str, Any]:
        logger.info(f"👑 [Manager] Starting SDLC Loop for request: {user_request[:50]}...")
        
        # Stage 1: Planning
        master_plan = await self.create_master_plan(user_request, current_code)
        
        # Optional Stage: UI Design if request mentions frontend/UI
        ui_spec = ""
        if any(w in user_request.lower() for w in ["ui", "interface", "dashboard", "css", "html", "style", "frontend"]):
            logger.info("🎨 [Manager] Task involves UI/UX. Calling UI/UX Designer Agent...")
            ui_spec = await self.designer.execute(f"Create UI/UX layout & CSS token spec for: {user_request}")

        # Stage 2: Initial Development
        logger.info("💻 [Manager] Assigning code implementation to Dev Agent...")
        code = await self.dev.generate_code(master_plan=master_plan, ui_spec=ui_spec, current_code=current_code)

        # Stage 3: Iterative Review & Feedback Loop
        iteration = 0
        loop_history = []
        is_approved = False

        while iteration < max_retries:
            iteration += 1
            logger.info(f"🔄 [Loop Iteration {iteration}/{max_retries}] Auditing code quality & security...")

            # Run Security Audit & QA Tests
            sec_res = await self.security.audit(code)
            qa_res = await self.qa.test_code(code)

            loop_history.append({
                "iteration": iteration,
                "sec_passed": sec_res["is_secure"],
                "qa_passed": qa_res["all_passed"],
                "sec_issues": sec_res["issues"],
                "qa_errors": qa_res["errors"]
            })

            # Check for Approval (0 flaws)
            if sec_res["is_secure"] and qa_res["all_passed"]:
                logger.info("✅ [APPROVED] Code passed security audit and QA tests with ZERO flaws!")
                is_approved = True
                break

            # If rejected -> Generate Bug Report and trigger Dev Fix
            bugs_summary = (
                f"Security Vulnerabilities: {sec_res['issues']}\n"
                f"Security Recs: {sec_res['recommendations']}\n"
                f"QA Errors / Missing Validations: {qa_res['errors']}"
            )
            logger.warning(f"❌ [Rejected - Iteration {iteration}] Sending feedback to Dev Agent for auto-fix...")
            code = await self.dev.fix_code(current_code=code, bug_report=bugs_summary, master_plan=master_plan)

        return {
            "status": "APPROVED" if is_approved else "REACHED_MAX_RETRIES",
            "iterations_used": iteration,
            "master_plan": master_plan,
            "ui_spec": ui_spec,
            "final_code": code,
            "sec_audit": sec_res if 'sec_res' in locals() else {},
            "qa_report": qa_res if 'qa_res' in locals() else {},
            "loop_history": loop_history
        }
