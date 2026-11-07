"""
Handles the creation and linking of chat sessions between the application 
metadata store and the AgentCore Memory service.
"""

import uuid
import boto3
import json
from typing import Dict, Any, Optional
from config import settings

class SessionCreator:
    """Manages the lifecycle of starting a new chat session."""

    def __init__(self, table_name: str = "ChatSessions"):
        # DynamoDB for metadata (User <-> Session mapping)
        self.db = boto3.resource("dynamodb")
        self.table = self.db.Table(table_name)
        
        # Bedrock Agent Runtime for executing the first message
        self.bedrock = boto3.client(
            "bedrock-agent-runtime", 
            region_name=settings.AWS_REGION
        )

    def create_new_session(self, user_id: str, initial_message: str, agent_key: str = "snow_agent") -> Dict[str, Any]:
        """
        Ties a new session to a user and triggers the first AgentCore invocation.
        """
        # 1. Generate a unique Session ID
        session_id = str(uuid.uuid4())

        # 2. Store session metadata in DynamoDB
        # This ensures we can retrieve this session ID later for the user's history list
        self.table.put_item(
            Item={
                "actor_id": user_id,
                "session_id": session_id,
                "title": initial_message[:50] + "..." if len(initial_message) > 50 else initial_message,
                "created_at": uuid.uuid1().hex, # simplified timestamp
                "updated_at": uuid.uuid1().hex,
                "agent": agent_key
            }
        )

        # 3. Invoke AgentCore with the new sessionId
        # AgentCore Memory will now automatically create a bucket for this sessionId
        try:
            response = self.bedrock.invoke_agent_runtime(
                agentRuntimeArn=settings.AGENT_RUNTIME_ARN, # Assumes this is in config.py
                runtimeSessionId=session_id,
                payload=json.dumps({
                    "prompt": initial_message,
                    "agent": agent_key
                }).encode("utf-8"),
                contentType="application/json",
                accept="application/json",
            )
            
            response_body = json.loads(response.get("body").read())
            return {
                "status": "success",
                "session_id": session_id,
                "answer": response_body.get("result"),
                "metadata": {
                    "user_id": user_id,
                    "agent": agent_key
                }
            }
        except Exception as e:
            print(f"Error initiating AgentCore session: {e}")
            return {"status": "error", "message": str(e), "session_id": session_id}

# ==========================================
# Example Usage
# ==========================================
if __name__ == "__main__":
    # Configuration
    USER_ID = "user_12345"
    FIRST_PROMPT = "Hello, I need help with a VPN issue."
    
    creator = SessionCreator()
    print(f"Creating new session for {USER_ID}...")
    
    result = creator.create_new_session(USER_ID, FIRST_PROMPT)
    
    if result["status"] == "success":
        print(f"Session Created! ID: {result['session_id']}")
        print(f"First Agent Response: {result['answer']}")
    else:
        print(f"Failed to create session: {result['message']}")
