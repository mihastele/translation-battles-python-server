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
import random

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
    countdown_active: bool = False
    countdown_remaining: int = 0

class CreateLobbyRequest(BaseModel):
    name: str
    gameMode: str
    maxPlayers: int = 4
    host: str  # User ID of the host
    username: str  # Username of the host

class JoinLobbyRequest(BaseModel):
    userId: str
    username: str

class SetReadyRequest(BaseModel):
    userId: str
    status: str

class LeaveLobbyRequest(BaseModel):
    userId: str

class StartGameRequest(BaseModel):
    userId: str

@dataclass
class Lobby:
    id: str
    name: str
    host: str
    players: List[Player]
    game_mode: str
    max_players: int
    status: str = "waiting"  # waiting, countdown, in_progress, ended
    created: str = None
    countdown_active: bool = False
    countdown_task: Optional[asyncio.Task] = None
    
    def __post_init__(self):
        if self.created is None:
            self.created = datetime.now().isoformat()

class GameState:
    def __init__(self):
        self.lobbies: Dict[str, Lobby] = {}
        self.connections: Dict[str, WebSocket] = {}  # player_id -> websocket
        self.player_lobbies: Dict[str, str] = {}  # player_id -> lobby_id
        self.countdown_tasks: Dict[str, asyncio.Task] = {}  # lobby_id -> countdown_task
        
    def add_lobby(self, lobby: Lobby):
        self.lobbies[lobby.id] = lobby
        
    def get_lobby(self, lobby_id: str) -> Optional[Lobby]:
        return self.lobbies.get(lobby_id)
        
    def remove_lobby(self, lobby_id: str):
        if lobby_id in self.lobbies:
            # Cancel any active countdown
            if lobby_id in self.countdown_tasks:
                self.countdown_tasks[lobby_id].cancel()
                del self.countdown_tasks[lobby_id]
            del self.lobbies[lobby_id]
            
    def add_player_connection(self, player_id: str, websocket: WebSocket):
        self.connections[player_id] = websocket
        
    def remove_player_connection(self, player_id: str):
        if player_id in self.connections:
            del self.connections[player_id]
            
    def get_player_connection(self, player_id: str) -> Optional[WebSocket]:
        return self.connections.get(player_id)
        
    def set_countdown_task(self, lobby_id: str, task: asyncio.Task):
        self.countdown_tasks[lobby_id] = task
        
    def cancel_countdown_task(self, lobby_id: str):
        if lobby_id in self.countdown_tasks:
            self.countdown_tasks[lobby_id].cancel()
            del self.countdown_tasks[lobby_id]

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

    async def send_to_player(self, player_id: str, message: dict):
        """Send message to a specific player"""
        websocket = game_state.get_player_connection(player_id)
        if websocket:
            try:
                message_str = json.dumps(message)
                await websocket.send_text(message_str)
            except Exception as e:
                logger.error(f"Error sending message to player {player_id}: {e}")

manager = ConnectionManager()

# Countdown functionality
async def start_countdown_for_unready_players(lobby_id: str):
    """Start a 15-second countdown for unready players"""
    lobby = game_state.get_lobby(lobby_id)
    if not lobby:
        return
        
    # Find unready players
    unready_players = [p for p in lobby.players if p.status != "ready"]
    if not unready_players:
        # All players are ready, start game immediately
        await start_game_immediately(lobby_id)
        return
    
    # Set countdown for each unready player
    for player in unready_players:
        player.countdown_active = True
        player.countdown_remaining = 15
    
    lobby.countdown_active = True
    lobby.status = "countdown"
    
    # Broadcast countdown start
    await manager.broadcast_to_lobby(lobby_id, {
        "type": "countdown_started",
        "unreadyPlayers": [{"id": p.id, "username": p.username} for p in unready_players],
        "duration": 15
    })
    
    # Start the countdown task
    task = asyncio.create_task(countdown_timer(lobby_id))
    game_state.set_countdown_task(lobby_id, task)

async def countdown_timer(lobby_id: str):
    """Handle the countdown timer logic"""
    try:
        for remaining in range(15, 0, -1):
            await asyncio.sleep(1)
            
            lobby = game_state.get_lobby(lobby_id)
            if not lobby or not lobby.countdown_active:
                return
            
            # Update countdown for unready players
            unready_players = [p for p in lobby.players if p.status != "ready"]
            if not unready_players:
                # All players became ready, start game immediately
                lobby.countdown_active = False
                await start_game_immediately(lobby_id)
                return
            
            # Update remaining time for unready players
            for player in unready_players:
                player.countdown_remaining = remaining
            
            # Broadcast countdown update
            await manager.broadcast_to_lobby(lobby_id, {
                "type": "countdown_update",
                "remaining": remaining,
                "unreadyPlayers": [{"id": p.id, "username": p.username, "remaining": p.countdown_remaining} for p in unready_players]
            })
        
        # Countdown finished, start game with current players
        lobby = game_state.get_lobby(lobby_id)
        if lobby and lobby.countdown_active:
            lobby.countdown_active = False
            for player in lobby.players:
                player.countdown_active = False
                player.countdown_remaining = 0
            await start_game_immediately(lobby_id)
            
    except asyncio.CancelledError:
        # Countdown was cancelled (e.g., all players became ready)
        lobby = game_state.get_lobby(lobby_id)
        if lobby:
            lobby.countdown_active = False
            for player in lobby.players:
                player.countdown_active = False
                player.countdown_remaining = 0

async def start_game_immediately(lobby_id: str):
    """Start the game immediately"""
    lobby = game_state.get_lobby(lobby_id)
    if not lobby:
        return
        
    lobby.status = "in_progress"
    lobby.countdown_active = False
    
    # Clean up countdown state
    game_state.cancel_countdown_task(lobby_id)
    for player in lobby.players:
        player.countdown_active = False
        player.countdown_remaining = 0
    
    await manager.broadcast_to_lobby(lobby_id, {
        "type": "game_started",
        "gameState": "in_progress"
    })
    
    logger.info(f"Game started in lobby {lobby_id}")

async def assign_new_host(lobby: Lobby):
    """Randomly assign a new host when the current host leaves"""
    if not lobby.players:
        return None
    
    # Remove host status from all players first
    for player in lobby.players:
        player.is_host = False
    
    # Randomly select a new host
    new_host = random.choice(lobby.players)
    new_host.is_host = True
    lobby.host = new_host.id
    
    return new_host

async def handle_player_leave(lobby_id: str, leaving_player_id: str):
    """Handle comprehensive player leave logic"""
    lobby = game_state.get_lobby(lobby_id)
    if not lobby:
        return
        
    # Find and remove the leaving player
    leaving_player = None
    for i, player in enumerate(lobby.players):
        if player.id == leaving_player_id:
            leaving_player = lobby.players.pop(i)
            break
    
    if not leaving_player:
        return
        
    # Remove from player_lobbies mapping
    if leaving_player_id in game_state.player_lobbies:
        del game_state.player_lobbies[leaving_player_id]
    
    # Remove player connection
    game_state.remove_player_connection(leaving_player_id)
    
    # If lobby is empty, remove it
    if not lobby.players:
        game_state.remove_lobby(lobby_id)
        logger.info(f"Removed empty lobby {lobby_id}")
        return
    
    # If the host left, assign new host
    new_host = None
    if leaving_player.is_host:
        new_host = await assign_new_host(lobby)
        
        # Broadcast host change
        await manager.broadcast_to_lobby(lobby_id, {
            "type": "host_changed",
            "newHost": {
                "id": new_host.id,
                "username": new_host.username
            },
            "players": [asdict(p) for p in lobby.players]
        })
    
    # Check if countdown should be cancelled (if all remaining players are ready)
    if lobby.countdown_active:
        unready_players = [p for p in lobby.players if p.status != "ready"]
        if not unready_players:
            lobby.countdown_active = False
            game_state.cancel_countdown_task(lobby_id)
            await start_game_immediately(lobby_id)
            return
    
    # Broadcast player left message
    await manager.broadcast_to_lobby(lobby_id, {
        "type": "player_left",
        "playerName": leaving_player.username,
        "playerId": leaving_player.id,
        "players": [asdict(p) for p in lobby.players],
        "newHost": new_host.username if new_host else None
    })
    
    logger.info(f"Player {leaving_player.username} left lobby {lobby_id}")

async def handle_player_ready_change(lobby_id: str, player_id: str, new_status: str):
    """Handle player ready status changes with countdown logic"""
    lobby = game_state.get_lobby(lobby_id)
    if not lobby:
        return False
        
    # Find and update player status
    player_found = False
    for player in lobby.players:
        if player.id == player_id:
            player.status = new_status
            player_found = True
            
            # If player became ready during countdown, update their countdown state
            if new_status == "ready" and player.countdown_active:
                player.countdown_active = False
                player.countdown_remaining = 0
            break
    
    if not player_found:
        return False
    
    # Check if all players are now ready
    all_ready = all(p.status == "ready" for p in lobby.players)
    
    if all_ready and lobby.countdown_active:
        # Cancel countdown and start game immediately
        lobby.countdown_active = False
        game_state.cancel_countdown_task(lobby_id)
        await start_game_immediately(lobby_id)
        return True
    elif all_ready and not lobby.countdown_active and len(lobby.players) > 1:
        # All players ready without countdown - could auto-start or wait for host
        pass
    
    # Broadcast status update
    await manager.broadcast_to_lobby(lobby_id, {
        "type": "player_ready",
        "playerId": player_id,
        "status": new_status,
        "players": [asdict(p) for p in lobby.players],
        "allReady": all_ready
    })
    
    return True

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
        "created": lobby.created,
        "countdownActive": lobby.countdown_active
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
        
        # Create host player using the actual user information
        host_player = Player(
            id=request.host,
            username=request.username,
            status="ready",  # Host starts ready
            is_host=True
        )
        
        # Create lobby
        lobby = Lobby(
            id=lobby_id,
            name=request.name,
            host=request.host,
            players=[host_player],
            game_mode=request.gameMode,
            max_players=request.maxPlayers
        )
        
        # Add the host to the player_lobbies mapping
        game_state.player_lobbies[request.host] = lobby_id
        
        game_state.add_lobby(lobby)
        
        logger.info(f"Created lobby {lobby_id}: {request.name} (Host: {request.username})")
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
            
        # Check if game is already in progress
        if lobby.status == "in_progress":
            raise HTTPException(status_code=400, detail="Game is already in progress")
            
        # Check if player is already in the lobby
        if any(p.id == request.userId for p in lobby.players):
            logger.info(f"Player {request.userId} already in lobby {lobby_id}")
            # Update the player_lobbies mapping in case it's missing
            game_state.player_lobbies[request.userId] = lobby_id
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
            "player": asdict(new_player),
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
            
        # Check if player is in the lobby
        player_in_lobby = any(p.id == request.userId for p in lobby.players)
        if not player_in_lobby:
            raise HTTPException(status_code=404, detail="Player not found in lobby")
        
        # Handle the leave operation
        await handle_player_leave(lobby_id, request.userId)
        
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
            
        # Use the new ready change handler
        success = await handle_player_ready_change(lobby_id, request.userId, request.status)
        
        if not success:
            raise HTTPException(status_code=404, detail="Player not found in lobby")
        
        # Return updated lobby state
        updated_lobby = game_state.get_lobby(lobby_id)
        if updated_lobby:
            logger.info(f"Player {request.userId} status updated to {request.status} in lobby {lobby_id}")
            return {"success": True, "lobby": lobby_to_dict(updated_lobby)}
        else:
            # Lobby might have been removed if game started
            return {"success": True, "message": "Status updated"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting player ready: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/lobbies/{lobby_id}/start")
async def start_game(lobby_id: str, request: StartGameRequest):
    """Start the game (host only) - with option for countdown if not all players ready"""
    try:
        lobby = game_state.get_lobby(lobby_id)
        if not lobby:
            raise HTTPException(status_code=404, detail="Lobby not found")
            
        # Check if requester is the host
        is_host = any(p.id == request.userId and p.is_host for p in lobby.players)
        if not is_host:
            raise HTTPException(status_code=403, detail="Only the host can start the game")
            
        # Check if game is already in progress
        if lobby.status in ["countdown", "in_progress"]:
            raise HTTPException(status_code=400, detail="Game is already started or starting")
            
        # Check if there are enough players
        if len(lobby.players) < 2:
            raise HTTPException(status_code=400, detail="Need at least 2 players to start")
        
        # Check if all players are ready
        all_ready = all(p.status == "ready" for p in lobby.players)
        
        if all_ready:
            # Start game immediately
            await start_game_immediately(lobby_id)
            return {"success": True, "message": "Game started immediately"}
        else:
            # Start countdown for unready players
            await start_countdown_for_unready_players(lobby_id)
            return {"success": True, "message": "Starting countdown for unready players"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting game: {e}")
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
                
                # Send current lobby state if player is in a lobby
                if player_id in game_state.player_lobbies:
                    current_lobby_id = game_state.player_lobbies[player_id]
                    current_lobby = game_state.get_lobby(current_lobby_id)
                    if current_lobby:
                        await websocket.send_text(json.dumps({
                            "type": "lobby_state",
                            "lobby": lobby_to_dict(current_lobby)
                        }))
                continue
            
            if action == "joinLobby":
                lobby = game_state.get_lobby(lobby_id)
                if lobby:
                    # Send confirmation and current lobby state
                    await websocket.send_text(json.dumps({
                        "type": "connected",
                        "lobbyId": lobby_id,
                        "lobby": lobby_to_dict(lobby)
                    }))
                    
            elif action == "startGame" and player_id:
                lobby = game_state.get_lobby(lobby_id)
                if lobby:
                    # Check if player is host
                    is_host = any(p.id == player_id and p.is_host for p in lobby.players)
                    if is_host:
                        # Check if all players are ready
                        all_ready = all(p.status == "ready" for p in lobby.players)
                        
                        if all_ready:
                            await start_game_immediately(lobby_id)
                        else:
                            await start_countdown_for_unready_players(lobby_id)
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": "Only the host can start the game"
                        }))
                        
            elif action == "toggleReady" and player_id:
                current_status = message.get("currentStatus", "not-ready")
                new_status = "not-ready" if current_status == "ready" else "ready"
                
                success = await handle_player_ready_change(lobby_id, player_id, new_status)
                if not success:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "Failed to update ready status"
                    }))
                    
            elif action == "leaveLobby" and player_id:
                await handle_player_leave(lobby_id, player_id)
                await websocket.send_text(json.dumps({
                    "type": "left_lobby"
                }))
                
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
            
            # Handle player disconnection as leaving the lobby
            if player_id in game_state.player_lobbies:
                lobby_id = game_state.player_lobbies[player_id]
                await handle_player_leave(lobby_id, player_id)

if __name__ == "__main__":
    uvicorn.run(
        "app:app", 
        host="0.0.0.0", 
        port=8000, 
        reload=True,
        log_level="info"
    )
