"""
Retrieves user chat sessions and their history from AgentCore Memory.

This module demonstrates the two-step process required to fetch history:
1. Retrieve session IDs from the application's metadata store (e.g., DynamoDB).
2. Retrieve actual message transcripts from the AgentCore Memory service.
"""

import boto3
from typing import List, Dict, Any
from dataclasses import dataclass

@dataclass
class ChatMessage:
    role: str
    content: str
    timestamp: str

class AgentCoreMemoryClient:
    """Client for interacting with the AgentCore Memory service."""
    def __init__(self, region: str = "us-east-1"):
        # Uses the bedrock-agent-runtime client to interact with AgentCore Memory
        self.client = boto3.client("bedrock-agent-runtime", region_name=region)

    def fetch_transcript(self, session_id: str, limit: int = 50) -> List[ChatMessage]:
        """Retrieves the actual message history for a specific sessionId."""
        try:
            # The 'list_events' operation returns the history for the sessionId
            response = self.client.list_events(
                sessionId=session_id, 
                maxResults=limit
            )
            
            events = response.get("events", [])
            return [
                ChatMessage(
                    role=e.get("role", "unknown"),
                    content=e.get("content", ""),
                    timestamp=e.get("timestamp", "")
                ) for e in events
            ]
        except Exception as e:
            print(f"Error fetching from AgentCore Memory: {e}")
            return []


class SessionMetadataStore:
    """Client for interacting with the application's session metadata store (DynamoDB)."""
    def __init__(self, table_name: str = "ChatSessions"):
        self.db = boto3.resource("dynamodb")
        self.table = self.db.Table(table_name)

    def get_sessions_for_user(self, user_id: str) -> List[Dict]:
        """Returns a list of all session IDs and metadata for a specific user."""
        try:
            response = self.table.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key('actor_id').eq(user_id)
            )
            return response.get("Items", [])
        except Exception as e:
            print(f"Error fetching metadata from DynamoDB: {e}")
            return []


class SessionManager:
    """Orchestrates the retrieval of session metadata and actual chat history."""
    def __init__(self, region: str = "us-east-1", table_name: str = "ChatSessions"):
        self.metadata = SessionMetadataStore(table_name)
        self.memory = AgentCoreMemoryClient(region)

    def get_user_history_list(self, user_id: str) -> List[Dict]:
        """Step 1: Get the list of available sessions for the user from the metadata store."""
        return self.metadata.get_sessions_for_user(user_id)

    def load_chat_session(self, user_id: str, session_id: str) -> List[ChatMessage]:
        """Step 2: Use the retrieved session_id to fetch messages from AgentCore Memory."""
        # In production, you should verify the session_id belongs to the user_id in the DB first.
        return self.memory.fetch_transcript(session_id)

# ==========================================
# Example Usage
# ==========================================
if __name__ == "__main__":
    # Replace these with your actual configuration
    REGION = "us-east-1"
    TABLE_NAME = "ChatSessions"
    USER_ID = "user_12345"

    manager = SessionManager(region=REGION, table_name=TABLE_NAME)

    print(f"--- Fetching sessions for user: {USER_ID} ---")
    sessions = manager.get_user_history_list(USER_ID)
    
    if not sessions:
        print("No sessions found for this user.")
    else:
        print(f"Found {len(sessions)} sessions.")
        # Load the first available session as an example
        target_id = sessions[0]["session_id"]
        print(f"Loading transcript for session: {target_id}...")
        
        history = manager.load_chat_session(USER_ID, target_id)
        if not history:
            print("No messages found in this session.")
        else:
            for msg in history:
                print(f"[{msg.timestamp}] {msg.role.upper()}: {msg.content[:80]}...")
