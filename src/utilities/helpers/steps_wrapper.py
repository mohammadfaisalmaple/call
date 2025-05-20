# ./src/utilities/helpers/steps_wrapper.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
steps_wrapper.py
---------------
Utility to execute steps with logging and error handling.
"""

from infrastructure.logging.logger import logger

def execute_step(step_name: str, step_func: callable, step_params: dict, operation_context: dict, mandatory: bool, description: str) -> bool:
    """
    Executes a step with logging and error handling.
    
    Args:
        step_name (str): Name of the step for logging.
        step_func (callable): Function to execute.
        step_params (dict): Parameters to pass to the function.
        operation_context (dict): Context for logging (e.g., operation_name, action).
        mandatory (bool): If True, failure will be logged as an error.
        description (str): Description of the step for logging.
    
    Returns:
        bool: True if the step executed successfully, False otherwise.
    """
    trace_logger = logger.bind(**operation_context)
    trace_logger.info(f"[execute_step] Starting {step_name}: {description}")
    
    try:
        step_func(**step_params)
        trace_logger.info(f"[execute_step] {step_name} completed successfully")
        return True
    except Exception as e:
        log_level = logger.error if mandatory else logger.warning
        log_level(f"[execute_step] {step_name} failed: {str(e)}")
        return False