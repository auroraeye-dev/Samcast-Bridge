"""Hand tracking on Windows via MediaPipe — counterpart to
`VisionHandTracker.swift`.

MediaPipe is the closest equivalent to Apple's Vision hand pose: it reports
the same 21 joints in the same order, which is why the ported classifier in
`gestures.py` can be a direct translation rather than a rewrite.

The camera runs on its own thread and hands the app only stable gestures —
never raw frames, and nothing is recorded, written to disk or transmitted.
The video never leaves this function.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from .gestures import Gesture, GestureClassifier, GestureDebouncer, HandLandmarks


class CameraUnavailable(Exception):
    """Raised with a message meant to be shown to the user verbatim."""


class HandTracker:
    """Watches the webcam and reports stable gestures."""

    def __init__(
        self,
        on_gesture: Callable[[Gesture], None],
        on_status: Optional[Callable[[str], None]] = None,
        camera_index: int = 0,
        hold_duration: float = 0.3,
    ) -> None:
        self.on_gesture = on_gesture
        self.on_status = on_status or (lambda message: None)
        self.camera_index = camera_index
        self._classifier = GestureClassifier()
        self._debouncer = GestureDebouncer(hold_duration=hold_duration)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        #: Latest raw (undebounced) gesture, purely for showing the user that
        #: the camera can see their hand.
        self.live_gesture: Gesture = Gesture.NONE
        self.hand_visible = False

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._debouncer.reset()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None
        self.hand_visible = False
        self.live_gesture = Gesture.NONE

    def _loop(self) -> None:
        try:
            import cv2
            import mediapipe as mp
        except ImportError as exc:
            self.on_status(
                f"Hand tracking needs {exc.name}. Run: pip install -r requirements.txt"
            )
            return

        capture = cv2.VideoCapture(self.camera_index, getattr(cv2, "CAP_DSHOW", 0))
        if not capture.isOpened():
            self.on_status("No camera found. QuackCast still receives without one.")
            return

        # 640×480 is plenty for hand tracking and keeps CPU use modest — this
        # runs continuously in the background, so it must not cost much.
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        hands = mp.solutions.hands.Hands(
            model_complexity=0,
            max_num_hands=2,
            min_detection_confidence=0.6,
            min_tracking_confidence=0.5,
        )
        self.on_status("Camera on — ✊ to grab, 🖐️ to take")

        try:
            while not self._stop.is_set():
                ok, frame = capture.read()
                if not ok:
                    time.sleep(0.05)
                    continue

                result = hands.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                now = time.monotonic()

                raw = Gesture.NONE
                landmarks = getattr(result, "multi_hand_landmarks", None)
                if landmarks:
                    confidence = 1.0
                    handedness = getattr(result, "multi_handedness", None)
                    if handedness:
                        try:
                            confidence = float(handedness[0].classification[0].score)
                        except Exception:
                            confidence = 1.0
                    hand = HandLandmarks.from_mediapipe(landmarks[0].landmark, confidence)
                    raw = self._classifier.classify(hand)

                self.hand_visible = bool(landmarks)
                self.live_gesture = raw

                stable = self._debouncer.update(raw, now)
                if stable is not None and stable != Gesture.NONE:
                    self.on_gesture(stable)
        finally:
            capture.release()
            try:
                hands.close()
            except Exception:
                pass
            self.hand_visible = False
            self.on_status("Camera off")
