#!/usr/bin/env python3
"""
Translation Battles Backend Server
A unified Python server handling both HTTP API and WebSocket connections
"""

import asyncio
import json
import uuid
import logging
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict
import weakref

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Data Models
@dataclass
class Player:
    id: str
    username: str
    status: str = "not-ready"  # not-ready, ready
    is_host: bool = False
    score: int = 0

class CreateLobbyRequest(BaseModel):
    name: str
    gameMode: str
    maxPlayers: int = 4

class JoinLobbyRequest(BaseModel):
    userId: str
    username: str

class SetReadyRequest(BaseModel):
    userId: str
    status: str

class LeaveLobbyRequest(BaseModel):
    userId: str

@dataclass
class Lobby:
    id: str
    name: str
    host: str
    players: List[Player]
    game_mode: str
    max_players: int
    status: str = "waiting"  # waiting, in_progress, ended
    created: str = None
    
    def __post_init__(self):
        if self.created is None:
            self.created = datetime.now().isoformat()

class GameState:
    def __init__(self):
        self.lobbies: Dict[str, Lobby] = {}
        self.connections: Dict[str, WebSocket] = {}  # player_id -> websocket
        self.player_lobbies: Dict[str, str] = {}  # player_id -> lobby_id
        
    def add_lobby(self, lobby: Lobby):
        self.lobbies[lobby.id] = lobby
        
    def get_lobby(self, lobby_id: str) -> Optional[Lobby]:
        return self.lobbies.get(lobby_id)
        
    def remove_lobby(self, lobby_id: str):
        if lobby_id in self.lobbies:
            del self.lobbies[lobby_id]
            
    def add_player_connection(self, player_id: str, websocket: WebSocket):
        self.connections[player_id] = websocket
        
    def remove_player_connection(self, player_id: str):
        if player_id in self.connections:
            del self.connections[player_id]
            
    def get_player_connection(self, player_id: str) -> Optional[WebSocket]:
        return self.connections.get(player_id)

# Global game state
game_state = GameState()

# FastAPI app
app = FastAPI(title="Translation Battles API", version="1.0.0")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def send_personal_message(self, message: str, websocket: WebSocket):
        try:
            await websocket.send_text(message)
        except Exception as e:
            logger.error(f"Error sending personal message: {e}")

    async def broadcast_to_lobby(self, lobby_id: str, message: dict):
        """Send message to all players in a specific lobby"""
        lobby = game_state.get_lobby(lobby_id)
        if not lobby:
            return
            
        message_str = json.dumps(message)
        for player in lobby.players:
            websocket = game_state.get_player_connection(player.id)
            if websocket:
                try:
                    await websocket.send_text(message_str)
                except Exception as e:
                    logger.error(f"Error broadcasting to player {player.id}: {e}")

manager = ConnectionManager()

# Helper functions
def lobby_to_dict(lobby: Lobby) -> dict:
    """Convert lobby to dictionary for JSON response"""
    return {
        "id": lobby.id,
        "name": lobby.name,
        "host": lobby.host,
        "players": [asdict(player) for player in lobby.players],
        "gameMode": lobby.game_mode,
        "maxPlayers": lobby.max_players,
        "status": lobby.status,
        "created": lobby.created
    }

# API Routes

@app.get("/")
async def root():
    return {"message": "Translation Battles API Server"}

@app.get("/lobbies")
async def get_lobbies():
    """Get list of all available lobbies"""
    lobbies = [lobby_to_dict(lobby) for lobby in game_state.lobbies.values()]
    return {"success": True, "lobbies": lobbies}

@app.post("/lobbies")
async def create_lobby(request: CreateLobbyRequest):
    """Create a new lobby"""
    try:
        # Generate unique lobby ID
        lobby_id = f"lobby_{uuid.uuid4().hex[:8]}"
        
        # Create host player (temporary ID - will be replaced when they connect via WebSocket)
        host_player = Player(
            id=f"user_{uuid.uuid4().hex[:8]}", 
            username="Host",
            status="ready",
            is_host=True
        )
        
        # Create lobby
        lobby = Lobby(
            id=lobby_id,
            name=request.name,
            host=host_player.id,
            players=[host_player],
            game_mode=request.gameMode,
            max_players=request.maxPlayers
        )
        
        game_state.add_lobby(lobby)
        
        logger.info(f"Created lobby {lobby_id}: {request.name}")
        return {"success": True, "lobby": lobby_to_dict(lobby)}
        
    except Exception as e:
        logger.error(f"Error creating lobby: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/lobbies/{lobby_id}/join")
async def join_lobby(lobby_id: str, request: JoinLobbyRequest):
    """Join an existing lobby"""
    try:
        lobby = game_state.get_lobby(lobby_id)
        if not lobby:
            raise HTTPException(status_code=404, detail="Lobby not found")
            
        # Check if lobby is full
        if len(lobby.players) >= lobby.max_players:
            raise HTTPException(status_code=400, detail="Lobby is full")
            
        # Check if player is already in the lobby
        if any(p.id == request.userId for p in lobby.players):
            logger.info(f"Player {request.userId} already in lobby {lobby_id}")
            return {"success": True, "lobby": lobby_to_dict(lobby)}
            
        # Add player to lobby
        new_player = Player(
            id=request.userId,
            username=request.username,
            status="not-ready"
        )
        lobby.players.append(new_player)
        game_state.player_lobbies[request.userId] = lobby_id
        
        # Broadcast player joined message
        await manager.broadcast_to_lobby(lobby_id, {
            "type": "player_joined",
            "playerName": request.username,
            "players": [asdict(p) for p in lobby.players],
            "gameState": lobby.status
        })
        
        logger.info(f"Player {request.username} joined lobby {lobby_id}")
        return {"success": True, "lobby": lobby_to_dict(lobby)}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error joining lobby: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/lobbies/{lobby_id}/leave")
async def leave_lobby(lobby_id: str, request: LeaveLobbyRequest):
    """Leave a lobby"""
    try:
        lobby = game_state.get_lobby(lobby_id)
        if not lobby:
            raise HTTPException(status_code=404, detail="Lobby not found")
            
        # Find and remove player
        player_to_remove = None
        for i, player in enumerate(lobby.players):
            if player.id == request.userId:
                player_to_remove = lobby.players.pop(i)
                break
                
        if not player_to_remove:
            raise HTTPException(status_code=404, detail="Player not found in lobby")
            
        # Remove from player_lobbies mapping
        if request.userId in game_state.player_lobbies:
            del game_state.player_lobbies[request.userId]
            
        # If lobby is empty, remove it
        if not lobby.players:
            game_state.remove_lobby(lobby_id)
            logger.info(f"Removed empty lobby {lobby_id}")
        else:
            # If the host left, assign new host
            if player_to_remove.is_host and lobby.players:
                lobby.players[0].is_host = True
                lobby.host = lobby.players[0].id
                
            # Broadcast player left message
            await manager.broadcast_to_lobby(lobby_id, {
                "type": "player_left",
                "playerName": player_to_remove.username,
                "players": [asdict(p) for p in lobby.players]
            })
        
        logger.info(f"Player {player_to_remove.username} left lobby {lobby_id}")
        return {"success": True, "message": "Left lobby successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error leaving lobby: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/lobbies/{lobby_id}/ready")
async def set_player_ready(lobby_id: str, request: SetReadyRequest):
    """Set player ready status"""
    try:
        lobby = game_state.get_lobby(lobby_id)
        if not lobby:
            raise HTTPException(status_code=404, detail="Lobby not found")
            
        # Find and update player status
        player_found = False
        for player in lobby.players:
            if player.id == request.userId:
                player.status = request.status
                player_found = True
                break
                
        if not player_found:
            raise HTTPException(status_code=404, detail="Player not found in lobby")
            
        # Broadcast status update
        await manager.broadcast_to_lobby(lobby_id, {
            "type": "player_ready",
            "playerId": request.userId,
            "status": request.status,
            "players": [asdict(p) for p in lobby.players]
        })
        
        # Check if all players are ready to start game
        if all(p.status == "ready" for p in lobby.players) and len(lobby.players) > 1:
            lobby.status = "in_progress"
            await manager.broadcast_to_lobby(lobby_id, {
                "type": "game_started",
                "gameState": "in_progress"
            })
        
        logger.info(f"Player {request.userId} status updated to {request.status} in lobby {lobby_id}")
        return {"success": True, "lobby": lobby_to_dict(lobby)}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting player ready: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    player_id = None
    
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            
            action = message.get("action")
            lobby_id = message.get("lobbyId")
            player_name = message.get("playerName", "Anonymous")
            
            # Store player connection for first message
            if action == "connect" and "playerId" in message:
                player_id = message["playerId"]
                game_state.add_player_connection(player_id, websocket)
                logger.info(f"WebSocket connected for player {player_id}")
                continue
            
            if action == "joinLobby":
                lobby = game_state.get_lobby(lobby_id)
                if lobby:
                    # This is handled by the HTTP API, WebSocket just confirms connection
                    await websocket.send_text(json.dumps({
                        "type": "connected",
                        "lobbyId": lobby_id
                    }))
                    
            elif action == "startGame":
                lobby = game_state.get_lobby(lobby_id)
                if lobby:
                    lobby.status = "in_progress"
                    await manager.broadcast_to_lobby(lobby_id, {
                        "type": "game_started",
                        "gameState": "in_progress"
                    })
                    
            elif action == "submitAnswer":
                answer = message.get("answer", "")
                await manager.broadcast_to_lobby(lobby_id, {
                    "type": "answer_submitted",
                    "playerName": player_name,
                    "answer": answer
                })
                
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for player {player_id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        manager.disconnect(websocket)
        if player_id:
            game_state.remove_player_connection(player_id)
            
            # Remove player from their lobby
            if player_id in game_state.player_lobbies:
                lobby_id = game_state.player_lobbies[player_id]
                lobby = game_state.get_lobby(lobby_id)
                if lobby:
                    # Remove player from lobby
                    lobby.players = [p for p in lobby.players if p.id != player_id]
                    del game_state.player_lobbies[player_id]
                    
                    if not lobby.players:
                        game_state.remove_lobby(lobby_id)
                    else:
                        await manager.broadcast_to_lobby(lobby_id, {
                            "type": "player_left",
                            "playerId": player_id,
                            "players": [asdict(p) for p in lobby.players]
                        })

if __name__ == "__main__":
    uvicorn.run(
        "app:app", 
        host="0.0.0.0", 
        port=8000, 
        reload=True,
        log_level="info"
    )
