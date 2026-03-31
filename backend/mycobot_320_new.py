"""
myCobot 320 Pi Bridge — WebSocket Server
Lightweight bridge between FastAPI backend and myCobot hardware.
Uses adaptive telemetry polling: 2Hz idle, 10Hz moving, 0Hz disconnected.
"""

import asyncio
import json
import logging
import time
from websockets.server import serve
from pymycobot import MyCobot320
import serial.tools.list_ports

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_mycobot_port():
    """Auto-detect myCobot serial port"""
    ports = list(serial.tools.list_ports.comports())
    if ports:
        port = ports[0].device
        logger.info(f"Auto-detected port: {port}")
        return port
    logger.warning("No ports detected, using /dev/ttyAMA0")
    return "/dev/ttyAMA0"


class MyCobotBridge:
    def __init__(self, port=None, baudrate=115200):
        if port is None:
            port = get_mycobot_port()

        try:
            self.mc = MyCobot320(port, baudrate)
            self.mc.power_on()
            logger.info(f"myCobot 320 connected on {port} at {baudrate} baud")
        except Exception as e:
            logger.error(f"Failed to connect to myCobot: {e}")
            self.mc = None

        self.client_connected = False
        self.poll_interval_idle = 0.5      # 2Hz
        self.poll_interval_moving = 0.1    # 10Hz
        self._speed = 50
        self._acceleration = 30

    def is_connected(self):
        return self.mc is not None

    def execute_command(self, data: dict) -> dict:
        """Execute a single command and return response"""
        if not self.mc:
            return {"type": "response", "status": "error", "message": "Robot not connected"}

        action = data.get("action", "")
        try:
            if action == "send_angles":
                angles = data.get("angles", [0]*6)
                speed = data.get("speed", self._speed)
                self.mc.send_angles(angles, speed)
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "send_angle":
                joint = data.get("joint", 1)
                angle = data.get("angle", 0)
                speed = data.get("speed", self._speed)
                self.mc.send_angle(joint, angle, speed)
                return {"type": "response", "action": action, "status": "ok", "joint": joint, "angle": angle}

            elif action == "send_coords":
                coords = data.get("coords", [0]*6)
                speed = data.get("speed", self._speed)
                self.mc.send_coords(coords, speed, 0)
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "jog_angle":
                joint = data.get("joint", 1)
                direction = data.get("direction", 1)
                speed = data.get("speed", 30)
                self.mc.jog_angle(joint, direction, speed)
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "jog_coord":
                axis = data.get("axis", 1)
                direction = data.get("direction", 1)
                speed = data.get("speed", 30)
                self.mc.jog_coord(axis, direction, speed)
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "jog_stop":
                self.mc.stop()
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "power_on":
                self.mc.power_on()
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "power_off":
                self.mc.power_off()
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "stop":
                self.mc.stop()
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "home":
                self.mc.send_angles([0]*6, 25)
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "set_speed":
                self._speed = data.get("speed", 50)
                return {"type": "response", "action": action, "status": "ok", "speed": self._speed}

            elif action == "set_acceleration":
                self._acceleration = data.get("acceleration", 30)
                return {"type": "response", "action": action, "status": "ok", "acceleration": self._acceleration}

            elif action == "set_gripper":
                value = data.get("value", 50)
                speed = data.get("speed", 50)
                self.mc.set_gripper_value(value, speed)
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "release_servo":
                joint = data.get("joint", 1)
                self.mc.release_servo(joint)
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "focus_servo":
                joint = data.get("joint", 1)
                self.mc.focus_servo(joint)
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "release_all_servos":
                self.mc.release_all_servos()
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "get_angles":
                angles = self.mc.get_angles()
                return {"type": "response", "action": action, "status": "ok", "angles": angles}

            elif action == "get_coords":
                coords = self.mc.get_coords()
                return {"type": "response", "action": action, "status": "ok", "coords": coords}

            else:
                return {"type": "response", "action": action, "status": "error", "message": f"Unknown action: {action}"}

        except Exception as e:
            logger.error(f"Error executing {action}: {e}")
            return {"type": "response", "action": action, "status": "error", "message": str(e)}

    def get_telemetry(self) -> dict:
        """Read current robot state"""
        if not self.mc:
            return {
                "type": "telemetry",
                "angles": [0]*6,
                "coords": [0]*6,
                "speed": self._speed,
                "acceleration": self._acceleration,
                "is_moving": False,
                "is_powered": False,
            }

        try:
            angles = self.mc.get_angles() or [0]*6
            coords = self.mc.get_coords() or [0]*6
            is_moving = self.mc.is_moving() or False
            is_powered = self.mc.is_power_on() or False

            return {
                "type": "telemetry",
                "angles": angles,
                "coords": coords,
                "speed": self._speed,
                "acceleration": self._acceleration,
                "is_moving": is_moving,
                "is_powered": is_powered,
            }
        except Exception as e:
            logger.error(f"Telemetry read error: {e}")
            return {
                "type": "telemetry",
                "angles": [0]*6,
                "coords": [0]*6,
                "speed": self._speed,
                "acceleration": self._acceleration,
                "is_moving": False,
                "is_powered": False,
            }


bridge = None


async def telemetry_loop(websocket):
    """Adaptive telemetry streaming"""
    while bridge.client_connected:
        telemetry = bridge.get_telemetry()
        try:
            await websocket.send(json.dumps(telemetry))
        except Exception:
            break

        is_moving = telemetry.get("is_moving", False)
        interval = bridge.poll_interval_moving if is_moving else bridge.poll_interval_idle
        await asyncio.sleep(interval)


async def handle_connection(websocket):
    """Handle WebSocket connection from FastAPI backend"""
    global bridge
    logger.info("Backend connected")

    # Send welcome
    await websocket.send(json.dumps({
        "type": "connected",
        "message": "myCobot 320 Pi bridge ready",
        "robot": "myCobot 320 Pi",
    }))

    bridge.client_connected = True

    # Start telemetry loop as a task
    telemetry_task = asyncio.create_task(telemetry_loop(websocket))

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                if data.get("type") == "command":
                    response = bridge.execute_command(data)
                    await websocket.send(json.dumps(response))
                else:
                    response = bridge.execute_command(data)
                    await websocket.send(json.dumps(response))
            except json.JSONDecodeError:
                await websocket.send(json.dumps({
                    "type": "response", "status": "error", "message": "Invalid JSON"
                }))
            except Exception as e:
                await websocket.send(json.dumps({
                    "type": "response", "status": "error", "message": str(e)
                }))
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        bridge.client_connected = False
        telemetry_task.cancel()
        logger.info("Backend disconnected")


async def main():
    global bridge
    bridge = MyCobotBridge(baudrate=115200)

    HOST = "0.0.0.0"
    PORT = 8765

    logger.info(f"myCobot 320 Pi bridge starting on {HOST}:{PORT}")
    async with serve(handle_connection, HOST, PORT):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        if bridge and bridge.mc:
            bridge.mc.power_off()
