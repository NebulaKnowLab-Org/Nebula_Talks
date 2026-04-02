"""
myCobot 320 Pi Bridge — WebSocket Server
Lightweight bridge between FastAPI backend and myCobot hardware.
Uses adaptive telemetry polling: 2Hz idle, 5Hz moving, 0Hz disconnected.

Key design decisions:
- send_coords() uses mode=1 (moveL) for Cartesian linear planning.
- jog_increment_coord() uses the native firmware command (0x34)
  instead of manual read-modify-send.
- set_fresh_mode(0): queue mode for stable Cartesian paths.
- set_vision_mode(1): reduce posture flipping in refresh-related paths.
- ALL serial I/O runs in thread pool — event loop never blocks.
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

# Streaming actions use latest-wins command slot.
# Only the most recent command is dispatched; stale ones are dropped.
STREAMING_ACTIONS = {
    "send_angles",
    "send_angle",
    "jog_increment_coord",
    "jog_increment_angle",
}


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
            time.sleep(0.5)
            # Coordinate system: 0 = base frame
            self.mc.set_reference_frame(0)
            time.sleep(0.1)
            # moveL: linear interpolation for straight-line Cartesian paths.
            # Also acts as the default if send_coords is called without mode.
            self.mc.set_movement_type(1)
            time.sleep(0.1)
            # Fresh mode 0: execute commands in sequence. This avoids
            # latest-command interruptions that can bend manual Cartesian paths.
            try:
                self.mc.set_fresh_mode(0)
                time.sleep(0.1)
            except AttributeError:
                logger.warning("set_fresh_mode not available — update pymycobot")
            # Vision mode 1: firmware option to reduce posture flipping when
            # Cartesian targets are updated in refresh-style control loops.
            try:
                self.mc.set_vision_mode(1)
                time.sleep(0.1)
            except AttributeError:
                logger.warning("set_vision_mode not available — update pymycobot")
            logger.info(f"myCobot 320 connected on {port} at {baudrate} baud")
            logger.info("Config: base frame, moveL, queue mode, vision mode on")
        except Exception as e:
            logger.error(f"Failed to connect to myCobot: {e}")
            self.mc = None

        self.client_connected = False
        self.poll_interval_idle = 0.5  # 2Hz
        self.poll_interval_moving = 0.2  # 5Hz
        self._speed = 50
        self._acceleration = 30
        self._last_angles = [0.0] * 6
        self._last_coords = [0.0] * 6
        self._precise_active = False
        self._precise_target = None
        self._precise_request_id = None
        self._precise_settle_count = 0
        self._precise_started_at = 0.0
        self._precise_axes_mask = [1, 1, 1, 1, 1, 1]
        self._precise_pos_tol_mm = 1.0
        self._precise_rot_tol_deg = 1.0
        self._precise_settle_samples = 3
        self._precise_timeout_s = 20.0

    def _clear_precise_move(self):
        self._precise_active = False
        self._precise_target = None
        self._precise_request_id = None
        self._precise_settle_count = 0
        self._precise_started_at = 0.0
        self._precise_axes_mask = [1, 1, 1, 1, 1, 1]

    @staticmethod
    def _angle_error_deg(target, current):
        diff = (target - current + 180.0) % 360.0 - 180.0
        return abs(diff)

    def _masked_errors(self, target, current, mask):
        m = (
            (list(mask) + [1, 1, 1, 1, 1, 1])[:6]
            if isinstance(mask, list)
            else [1, 1, 1, 1, 1, 1]
        )
        pos_indices = [i for i in range(3) if m[i]]
        rot_indices = [i for i in range(3, 6) if m[i]]
        pos_err = (
            max(abs(target[i] - current[i]) for i in pos_indices)
            if pos_indices
            else 0.0
        )
        rot_err = (
            max(self._angle_error_deg(target[i], current[i]) for i in rot_indices)
            if rot_indices
            else 0.0
        )
        return pos_err, rot_err

    @staticmethod
    def _extract_error_codes(raw):
        if raw is None or raw == -1:
            return []
        if isinstance(raw, int):
            return [raw] if raw > 0 else []
        if isinstance(raw, list):
            codes = []
            for item in raw:
                try:
                    code = int(item)
                except (TypeError, ValueError):
                    continue
                if code > 0:
                    codes.append(code)
            return sorted(set(codes))
        return []

    @staticmethod
    def _error_hint(codes):
        hints = {
            32: "IK no solution",
            33: "linear path no adjacent solution",
            34: "velocity blend error",
            35: "zero-space no adjacent solution",
            36: "singular position",
            49: "robot not enabled/powered",
            64: "target coordinate exceeds limit",
        }
        parts = [hints[c] for c in codes if c in hints]
        return ", ".join(parts) if parts else None

    def _wait_motion_start_or_reach(self, target, mask, timeout_s=1.5):
        latest_coords = self._last_coords
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            latest_coords = self.mc.get_coords() or latest_coords
            is_moving_now = bool(self.mc.is_moving())
            pos_err, rot_err = self._masked_errors(target, latest_coords, mask)
            if is_moving_now:
                return True, False, latest_coords
            if (
                pos_err <= self._precise_pos_tol_mm
                and rot_err <= self._precise_rot_tol_deg
            ):
                return False, True, latest_coords
            time.sleep(0.1)
        return False, False, latest_coords

    def _ensure_motion_ready(self):
        try:
            if int(self.mc.is_power_on()) != 1:
                self.mc.power_on()
                time.sleep(0.2)
        except Exception:
            pass

        try:
            enabled = int(self.mc.is_all_servo_enable())
        except Exception:
            enabled = 1

        if enabled != 1:
            for joint in range(1, 7):
                try:
                    self.mc.focus_servo(joint)
                except Exception:
                    continue
            time.sleep(0.15)

    def check_precise_completion(self, telemetry: dict):
        if not self._precise_active or not self._precise_target:
            return None

        coords = telemetry.get("coords", self._last_coords)
        is_moving = bool(telemetry.get("is_moving", False))

        pos_err, rot_err = self._masked_errors(
            self._precise_target, coords, self._precise_axes_mask
        )

        if (
            not is_moving
            and pos_err <= self._precise_pos_tol_mm
            and rot_err <= self._precise_rot_tol_deg
        ):
            self._precise_settle_count += 1
        else:
            self._precise_settle_count = 0

        if self._precise_settle_count >= self._precise_settle_samples:
            request_id = self._precise_request_id
            self._clear_precise_move()
            return {
                "type": "response",
                "action": "send_coords",
                "mode": "precise",
                "phase": "done",
                "status": "ok",
                "request_id": request_id,
            }

        if (
            self._precise_started_at
            and (time.time() - self._precise_started_at) > self._precise_timeout_s
        ):
            request_id = self._precise_request_id
            self._clear_precise_move()
            return {
                "type": "response",
                "action": "send_coords",
                "mode": "precise",
                "phase": "done",
                "status": "error",
                "message": "Precise move timeout",
                "request_id": request_id,
            }

        return None

    def is_connected(self):
        return self.mc is not None

    def execute_command(self, data: dict) -> dict:
        """Execute a single command and return response dict."""
        if not self.mc:
            return {
                "type": "response",
                "status": "error",
                "message": "Robot not connected",
            }

        action = data.get("action", "")
        try:
            if action == "send_angles":
                angles = data.get("angles", [0] * 6)
                speed = data.get("speed", self._speed)
                self.mc.send_angles(angles, speed)
                self._last_angles = angles
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "send_angle":
                joint = data.get("joint", 1)
                angle = data.get("angle", 0)
                speed = data.get("speed", self._speed)
                self.mc.send_angle(joint, angle, speed)
                return {
                    "type": "response",
                    "action": action,
                    "status": "ok",
                    "joint": joint,
                    "angle": angle,
                }

            elif action == "send_coords":
                raw_coords = data.get("coords", [0] * 6)
                coords = [float(v) for v in list(raw_coords)[:6]]
                if len(coords) < 6:
                    coords.extend([0.0] * (6 - len(coords)))
                speed = int(float(data.get("speed", self._speed)))
                mode = data.get("mode", "jog")
                request_id = data.get("request_id")
                axes_mask = data.get("axes_mask", [1, 1, 1, 1, 1, 1])
                # mode=1 does TWO things:
                #   1. Linear interpolation (moveL) — straight-line path
                #   2. Fire-and-forget at protocol level — no serial reply wait
                # Without mode, MyCobot320 blocks ~300s waiting for reply.
                if mode == "precise":
                    if self._precise_active:
                        return {
                            "type": "response",
                            "action": action,
                            "mode": "precise",
                            "phase": "accepted",
                            "status": "busy",
                            "message": "Precise move already in progress",
                            "request_id": request_id,
                        }

                    self._ensure_motion_ready()

                    if isinstance(axes_mask, list):
                        mask = [
                            1 if bool(v) else 0
                            for v in (axes_mask + [1, 1, 1, 1, 1, 1])[:6]
                        ]
                    else:
                        mask = [1, 1, 1, 1, 1, 1]

                    active_axes = [i for i, enabled in enumerate(mask) if enabled]
                    dispatch_attempts = []

                    if len(active_axes) == 1 and active_axes[0] < 3:
                        axis_idx = active_axes[0]
                        axis_id = axis_idx + 1
                        axis_target = float(coords[axis_idx])
                        dispatch_attempts.append(
                            (
                                "send_coord",
                                lambda: self.mc.send_coord(axis_id, axis_target, speed),
                            )
                        )

                        current_coords = self.mc.get_coords() or self._last_coords
                        delta = axis_target - float(current_coords[axis_idx])
                        if abs(delta) > 0.2:
                            dispatch_attempts.append(
                                (
                                    "jog_increment_coord",
                                    lambda d=delta: self.mc.jog_increment_coord(
                                        axis_id, d, speed
                                    ),
                                )
                            )
                        dispatch_attempts.append(
                            (
                                "send_coords_movej",
                                lambda: self.mc.send_coords(coords, speed, 0),
                            )
                        )
                    else:
                        dispatch_attempts.append(
                            (
                                "send_coords",
                                lambda: self.mc.send_coords(coords, speed, 1),
                            )
                        )
                        dispatch_attempts.append(
                            (
                                "send_coords_movej",
                                lambda: self.mc.send_coords(coords, speed, 0),
                            )
                        )

                    started = False
                    near_target = False
                    used_attempt = None
                    for name, dispatch in dispatch_attempts:
                        dispatch()
                        self._last_coords = coords
                        used_attempt = name
                        started, near_target, _ = self._wait_motion_start_or_reach(
                            coords, mask, 1.5
                        )
                        if started or near_target:
                            break

                    if near_target:
                        return {
                            "type": "response",
                            "action": action,
                            "mode": "precise",
                            "phase": "done",
                            "status": "ok",
                            "request_id": request_id,
                        }

                    if not started:
                        # Last fallback: solve IK from current joint state and move in
                        # joint space. This handles firmware cases where Cartesian
                        # commands are accepted but cannot start due to adjacency /
                        # singular constraints.
                        try:
                            current_angles = self.mc.get_angles() or self._last_angles
                        except Exception:
                            current_angles = self._last_angles

                        try:
                            ik_angles = self.mc.solve_inv_kinematics(
                                coords, current_angles
                            )
                        except Exception:
                            ik_angles = None

                        if isinstance(ik_angles, list) and len(ik_angles) == 6:
                            try:
                                ik_angles = [float(v) for v in ik_angles]
                                self.mc.send_angles(ik_angles, speed)
                                used_attempt = "solve_inv_kinematics+send_angles"
                                started, near_target, _ = (
                                    self._wait_motion_start_or_reach(coords, mask, 2.0)
                                )
                                if near_target:
                                    return {
                                        "type": "response",
                                        "action": action,
                                        "mode": "precise",
                                        "phase": "done",
                                        "status": "ok",
                                        "request_id": request_id,
                                    }
                            except Exception:
                                started = False

                        if started:
                            self._last_angles = (
                                ik_angles
                                if isinstance(ik_angles, list) and len(ik_angles) == 6
                                else self._last_angles
                            )
                        else:
                            started = False

                    if not started:
                        try:
                            err_info = self.mc.get_error_information()
                        except Exception:
                            err_info = None
                        err_codes = self._extract_error_codes(err_info)
                        hint = self._error_hint(err_codes)
                        suffix = (
                            f" (codes: {err_codes}; hint: {hint})" if err_codes else ""
                        )
                        return {
                            "type": "response",
                            "action": action,
                            "mode": "precise",
                            "phase": "done",
                            "status": "error",
                            "message": f"Move did not start after {used_attempt} (robot error: {err_info}){suffix}",
                            "request_id": request_id,
                        }

                    self._precise_active = True
                    self._precise_target = list(coords)
                    self._precise_request_id = request_id
                    self._precise_settle_count = 0
                    self._precise_started_at = time.time()
                    self._precise_axes_mask = mask
                    message = (
                        "Linear start failed; using moveJ fallback"
                        if used_attempt == "send_coords_movej"
                        else None
                    )
                    return {
                        "type": "response",
                        "action": action,
                        "mode": "precise",
                        "phase": "accepted",
                        "status": "ok",
                        "message": message,
                        "request_id": request_id,
                    }

                self.mc.send_coords(coords, speed, 1)
                self._last_coords = coords
                return {
                    "type": "response",
                    "action": action,
                    "mode": "jog",
                    "status": "ok",
                }

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

            elif action == "jog_increment_angle":
                joint = data.get("joint", 1)
                increment = data.get("angle", 1)
                speed = data.get("speed", 50)
                new_angles = list(self._last_angles)
                new_angles[joint - 1] += increment
                self.mc.send_angles(new_angles, speed)
                self._last_angles = new_angles
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "jog_increment_coord":
                axis = data.get("axis", 1)
                increment = data.get("value", 1)
                speed = data.get("speed", 50)
                # Native firmware command (protocol 0x34): moves one axis
                # by a relative increment. Much faster than read-modify-send
                # because the firmware handles it internally.
                self.mc.jog_increment_coord(axis, increment, speed)
                # Optimistically update cache
                new_coords = list(self._last_coords)
                new_coords[axis - 1] += increment
                self._last_coords = new_coords
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "jog_stop":
                self.mc.stop()
                self._clear_precise_move()
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "power_on":
                self.mc.power_on()
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "power_off":
                self.mc.power_off()
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "stop":
                self.mc.stop()
                self._clear_precise_move()
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "pause":
                self.mc.pause()
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "resume":
                self.mc.resume()
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "home":
                self.mc.send_angles([0] * 6, 25)
                self._last_angles = [0.0] * 6
                self._clear_precise_move()
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "set_speed":
                self._speed = data.get("speed", 50)
                return {
                    "type": "response",
                    "action": action,
                    "status": "ok",
                    "speed": self._speed,
                }

            elif action == "set_acceleration":
                self._acceleration = data.get("acceleration", 30)
                return {
                    "type": "response",
                    "action": action,
                    "status": "ok",
                    "acceleration": self._acceleration,
                }

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

            elif action == "focus_all_servos":
                for j in range(1, 7):
                    self.mc.focus_servo(j)
                return {"type": "response", "action": action, "status": "ok"}

            elif action == "get_angles":
                angles = self.mc.get_angles()
                if angles:
                    self._last_angles = angles
                return {
                    "type": "response",
                    "action": action,
                    "status": "ok",
                    "angles": angles or self._last_angles,
                }

            elif action == "get_coords":
                coords = self.mc.get_coords()
                if coords:
                    self._last_coords = coords
                return {
                    "type": "response",
                    "action": action,
                    "status": "ok",
                    "coords": coords or self._last_coords,
                }

            elif action == "get_speed":
                return {
                    "type": "response",
                    "action": action,
                    "status": "ok",
                    "speed": self._speed,
                }

            elif action == "get_acceleration":
                return {
                    "type": "response",
                    "action": action,
                    "status": "ok",
                    "acceleration": self._acceleration,
                }

            else:
                return {
                    "type": "response",
                    "action": action,
                    "status": "error",
                    "message": f"Unknown action: {action}",
                }

        except Exception as e:
            logger.error(f"Error executing {action}: {e}")
            return {
                "type": "response",
                "action": action,
                "status": "error",
                "message": str(e),
            }

    def get_telemetry(self) -> dict:
        """Read current robot state via serial."""
        if not self.mc:
            return {
                "type": "telemetry",
                "angles": [0] * 6,
                "coords": [0] * 6,
                "speed": self._speed,
                "acceleration": self._acceleration,
                "is_moving": False,
                "is_powered": False,
            }

        try:
            angles = self.mc.get_angles()
            if angles:
                self._last_angles = angles
            else:
                angles = self._last_angles

            coords = self.mc.get_coords()
            if coords:
                self._last_coords = coords
            else:
                coords = self._last_coords

            is_moving = self.mc.is_moving()
            is_powered = self.mc.is_power_on()

            return {
                "type": "telemetry",
                "angles": angles,
                "coords": coords,
                "speed": self._speed,
                "acceleration": self._acceleration,
                "is_moving": bool(is_moving),
                "is_powered": bool(is_powered),
            }
        except Exception as e:
            logger.error(f"Telemetry read error: {e}")
            return {
                "type": "telemetry",
                "angles": self._last_angles,
                "coords": self._last_coords,
                "speed": self._speed,
                "acceleration": self._acceleration,
                "is_moving": False,
                "is_powered": False,
            }


bridge = None


async def telemetry_loop(websocket):
    """Adaptive telemetry streaming.
    Serial reads run in thread pool so the event loop stays responsive.
    """
    loop = asyncio.get_event_loop()
    while bridge.client_connected:
        telemetry = await loop.run_in_executor(None, bridge.get_telemetry)
        try:
            await websocket.send(json.dumps(telemetry))
            precise_done = bridge.check_precise_completion(telemetry)
            if precise_done:
                await websocket.send(json.dumps(precise_done))
        except Exception:
            break

        is_moving = telemetry.get("is_moving", False)
        interval = (
            bridge.poll_interval_moving if is_moving else bridge.poll_interval_idle
        )
        await asyncio.sleep(interval)


async def handle_connection(websocket):
    """Handle WebSocket connection from FastAPI backend.

    Streaming commands (send_coords, jog_increment_coord, etc.) use a
    latest-wins slot — only the most recent command is dispatched.
    Non-streaming commands (power, home, stop) execute immediately.
    All serial I/O runs in a thread pool so telemetry is never blocked.
    """
    global bridge
    logger.info("Backend connected")

    await websocket.send(
        json.dumps(
            {
                "type": "connected",
                "message": "myCobot 320 Pi bridge ready",
                "robot": "myCobot 320 Pi",
            }
        )
    )

    bridge.client_connected = True

    # Latest-wins command slot for streaming actions
    command_slot = {"data": None}
    command_event = asyncio.Event()
    loop = asyncio.get_event_loop()

    async def command_consumer():
        """Execute the most recent streaming command, skipping stale ones."""
        try:
            while True:
                await command_event.wait()
                command_event.clear()

                cmd = command_slot["data"]
                command_slot["data"] = None
                if cmd is None:
                    continue

                try:
                    response = await loop.run_in_executor(
                        None, bridge.execute_command, cmd
                    )
                    if response:
                        await websocket.send(json.dumps(response))
                except Exception as e:
                    logger.error(f"Command consumer error: {e}")
        except asyncio.CancelledError:
            return

    # Start telemetry and command consumer tasks
    telemetry_task = asyncio.create_task(telemetry_loop(websocket))
    consumer_task = asyncio.create_task(command_consumer())

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                action = data.get("action", "")

                if action == "send_coords":
                    mode = data.get("mode", "jog")
                    if mode == "jog":
                        command_slot["data"] = data
                        command_event.set()
                    else:
                        response = await loop.run_in_executor(
                            None, bridge.execute_command, data
                        )
                        if response:
                            await websocket.send(json.dumps(response))
                elif action in STREAMING_ACTIONS:
                    # Latest command wins — overwrite slot
                    command_slot["data"] = data
                    command_event.set()
                else:
                    # Immediate commands: execute in thread pool
                    response = await loop.run_in_executor(
                        None, bridge.execute_command, data
                    )
                    if response:
                        await websocket.send(json.dumps(response))

            except json.JSONDecodeError:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "response",
                            "status": "error",
                            "message": "Invalid JSON",
                        }
                    )
                )
            except Exception as e:
                await websocket.send(
                    json.dumps(
                        {"type": "response", "status": "error", "message": str(e)}
                    )
                )
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        bridge.client_connected = False
        telemetry_task.cancel()
        consumer_task.cancel()
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
