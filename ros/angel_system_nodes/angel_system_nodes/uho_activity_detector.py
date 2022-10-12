from builtin_interfaces.msg import Time
from collections import deque
from cv_bridge import CvBridge
import cv2
from dataclasses import dataclass, field
import itertools
import numpy as np
import numpy.typing as npt
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from threading import RLock
from typing import Deque
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple

from angel_msgs.msg import (
    ActivityDetection,
    HandJointPosesUpdate,
    ObjectDetection2dSet
)

from angel_system.uho.predict import get_uho_classifier, predict
from angel_system.uho.src.data_helper import create_batch
from angel_system.utils.matching import descending_match_with_tolerance
from angel_utils.conversion import get_hand_pose_from_msg
from angel_utils.sync_msgs import get_frame_synced_hand_poses, match_hands_to_frames
from angel_utils.conversion import time_to_int


BRIDGE = CvBridge()


@dataclass(frozen=True)
class InputWindow:
    """
    Structure encapsulating a window of aligned data.
    """
    # Buffer of RGB image matrices and the associated timestamp.
    # Set at construction time with the known window of frames.
    frames: List[Tuple[Time, npt.NDArray]]
    # Buffer of left-hand pose messages
    hand_pose_left: List[Optional[HandJointPosesUpdate]]
    # Buffer of right-hand pose messages
    hand_pose_right: List[Optional[HandJointPosesUpdate]]
    # Buffer of object detection predictions
    obj_dets: List[Optional[ObjectDetection2dSet]]

    def __len__(self):
        return len(self.frames)


@dataclass(frozen=True)
class InputBuffer:
    """
    Protected container for buffering input data.

    Frames are the primary index of this buffer to which everything else needs
    to associate to.

    Hand pose sensor outputs are known to be output asynchronously and at a
    different rate than the images. Thus, we cannot presume there will be any
    hand messages that directly align with an image. To associate a message
    with the nearest image, we require a tolerance such that messages closer
    than this value to an image may be considered "associated" with the image.

    Object detection outputs are known to correlate strictly with an image
    frame via the timestamp value.

    Contained deques are in ascending time order (later indices are farther
    ahead in time).

    NOTE: `__post_init__` is a thing if we need it.
    """
    # Tolerance in nanoseconds for associating hand-pose messages to a frame.
    hand_msg_tolerance_nsec: int

    # Buffer of RGB image matrices and the associated timestamp
    frames: Deque[Tuple[Time, npt.NDArray]] = field(default_factory=deque,
                                                    init=False, repr=False)
    # Buffer of left-hand pose messages
    hand_pose_left: Deque[HandJointPosesUpdate] = field(default_factory=deque,
                                                        init=False, repr=False)
    # Buffer of right-hand pose messages
    hand_pose_right: Deque[HandJointPosesUpdate] = field(default_factory=deque,
                                                         init=False, repr=False)
    # Buffer of object detection predictions
    obj_dets: Deque[ObjectDetection2dSet] = field(default_factory=deque,
                                                  init=False, repr=False)

    __state_lock: RLock = field(default_factory=RLock, repr=False)

    def __enter__(self):
        """
        For when you want to call multiple things on the buffer in the same
        locking context.
        """
        # Same as RLock.__enter__
        self.__state_lock.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Same as RLock.__exit__
        self.__state_lock.release()

    def queue_image(self, msg: Image):
        # Convert ROS img msg to CV2 image and add it to the frame stack
        rgb_image = BRIDGE.imgmsg_to_cv2(msg, desired_encoding="rgb8")
        rgb_image_np = np.asarray(rgb_image)
        with self.__state_lock:
            self.frames.append((msg.header.stamp, rgb_image_np))

    def queue_hand_pose(self, msg: HandJointPosesUpdate) -> None:
        """
        Input hand pose may be of the left or right hand, as indicated by
        `msg.hand`.

        :param msg:
        :return:
        """
        hand_list: Deque[HandJointPosesUpdate]
        if msg.hand == 'Right':
            hand_list = self.hand_pose_right
        elif msg.hand == 'Left':
            hand_list = self.hand_pose_left
        else:
            raise ValueError(f"Input hand pose for hand '{msg.hand}'? What?")
        with self.__state_lock:
            hand_list.append(msg)

    def queue_object_detections(self, msg: ObjectDetection2dSet) -> None:
        """
        Queue up an object detection set for the
        :param msg:
        :return:
        """
        with self.__state_lock:
            self.obj_dets.append(msg)

    def get_frame_time(self, buff_index=-1) -> Time:
        """
        Get the timestamp of the image frame at the given index.

        By default, we return the most recent image's timestamp.

        :param buff_index: Index into the frame buffer to return
        :raises IndexError: Invalid index specified.
        :return: Time message reference.
        """
        with self.__state_lock:
            return self.frames[buff_index][0]

    def get_window_detections(self, window_size: int) -> List[ObjectDetection2dSet]:
        """
        Get the list of detection messages in the window as considered from the
        end of the buffer.
        """
        # We know that object detections are
        with self.__state_lock:
            # This window's frame in ascending time order
            window_frame_times_ns: List[int] = [
                time_to_int(f_time)
                for f_time, _,
                # deques don't support slicing, so thus the following madness
                in list(itertools.islice(reversed(self.frames), window_size))[::-1]
            ]

            window_dets = descending_match_with_tolerance(
                window_frame_times_ns,
                self.obj_dets,
                0,  # we expect exact matches for object detections.
                time_from_value_fn=self._objdet_msg_to_time_ns,
            )

            return window_dets

    @staticmethod
    def _hand_msg_to_time_ns(msg: HandJointPosesUpdate):
        return time_to_int(msg.header.stamp)

    @staticmethod
    def _objdet_msg_to_time_ns(msg: ObjectDetection2dSet):
        # Using stamp that should associate to the source image
        return time_to_int(msg.source_stamp)

    def get_window(self, window_size: int) -> InputWindow:
        """
        Get a window buffered data as it is associated to frame data.

        Data other than the image frames may not have direct association to a
        particular frame, e.g. "missing" for that frame. In those cases there
        will be a None in the applicable slot.

        :param window_size: number of frames from the head of the buffer to
            consider "the window."

        :return: Mapping of associated data, each of window_size items.
        """
        # Knowns:
        # - Object detections occur on a specific frame as associated by
        #   timestamp *exactly*.

        with self.__state_lock:
            # Cache self accesses
            hand_nsec_tol = self.hand_msg_tolerance_nsec

            # This window's frame in ascending time order
            # deques don't support slicing, so thus the following madness
            window_frames = list(itertools.islice(reversed(self.frames), window_size))[::-1]
            window_frame_times: List[Time] = [wf[0] for wf in window_frames]
            window_frame_times_ns: List[int] = [time_to_int(wft) for wft
                                                in window_frame_times]

            # tolerance associate hand messages, left and right
            # - For each frame backwards, reverse-iterate through hand messages
            #   until encountering one that is more time-distance or out of
            #   tolerance, which triggers moving on to the next frame.
            # - carry variable for when the item being checked in previous
            #   iteration did not match the current frame.
            window_lhand = descending_match_with_tolerance(
                window_frame_times_ns,
                self.hand_pose_left,
                hand_nsec_tol,
                time_from_value_fn=self._hand_msg_to_time_ns,
            )
            window_rhand = descending_match_with_tolerance(
                window_frame_times_ns,
                self.hand_pose_right,
                hand_nsec_tol,
                time_from_value_fn=self._hand_msg_to_time_ns,
            )

            # Direct associate object detections within window time. For
            # detections known to be in the window, creating a mapping (key=ts)
            # to access the detection for a specific time.
            window_dets = descending_match_with_tolerance(
                window_frame_times_ns,
                self.obj_dets,
                0,  # we expect exact matches for object detections.
                time_from_value_fn=self._objdet_msg_to_time_ns,
            )

            output = InputWindow(
                frames=window_frames,
                hand_pose_left=window_lhand,
                hand_pose_right=window_rhand,
                obj_dets=window_dets,
            )
            return output

    def clear_before(self, time_nsec: int) -> None:
        """
        Clear content in the buffer that is associate to a timestamp before the
        one given.
        """
        # for each deque, traverse from the left (the earliest time) and pop if
        # the ts is < the given.
        with self.__state_lock:
            while (self.frames and
                   time_to_int(self.frames[0][0]) < time_nsec):
                self.frames.popleft()
            while (self.hand_pose_left and
                   time_to_int(self.hand_pose_left[0].header.stamp) < time_nsec):
                self.hand_pose_left.popleft()
            while (self.hand_pose_right and
                   time_to_int(self.hand_pose_right[0].header.stamp) < time_nsec):
                self.hand_pose_right.popleft()
            while (self.obj_dets and
                   time_to_int(self.obj_dets[0].source_stamp) < time_nsec):
                self.obj_dets.popleft()


class UHOActivityDetector(Node):

    def __init__(self):
        super().__init__(self.__class__.__name__)

        # Declare ROS topics
        self._image_topic = (
            self.declare_parameter("image_topic", "PVFramesRGB")
            .get_parameter_value()
            .string_value
        )
        self._hand_topic = (
            self.declare_parameter("hand_pose_topic", "HandJointPoseData")
            .get_parameter_value()
            .string_value
        )
        self._obj_det_topic = (
            self.declare_parameter("obj_det_topic", "ObjectDetections")
            .get_parameter_value()
            .string_value
        )
        self._torch_device = (
            self.declare_parameter("torch_device", "cuda")
            .get_parameter_value()
            .string_value
        )
        self._det_topic = (
            self.declare_parameter("det_topic", "ActivityDetections")
            .get_parameter_value()
            .string_value
        )
        self._min_time_topic = (
            self.declare_parameter("min_time_topic", "ObjDetMinTime")
            .get_parameter_value()
            .string_value
        )
        self._frames_per_det = (
            self.declare_parameter("frames_per_det", 32)
            .get_parameter_value()
            .integer_value
        )
        # The number of object detections we require to be in the input buffer
        # window before considering it value for processing.
        self._obj_dets_per_window = (
            self.declare_parameter("object_dets_per_window", 2)
            .get_parameter_value()
            .integer_value
        )
        self._model_checkpoint = (
            self.declare_parameter("model_checkpoint",
                                   "/angel_workspace/model_files/uho_epoch_090.ckpt")
            .get_parameter_value()
            .string_value
        )
        self._labels_file = (
            self.declare_parameter("labels_file",
                                   "/angel_workspace/model_files/uho_epoch_090_labels.txt")
            .get_parameter_value()
            .string_value
        )
        # Model specific top-K parameter.
        self._topk = (
            self.declare_parameter("top_k", 5)
            .get_parameter_value()
            .integer_value
        )
        self._slop_ns = (5 / 60.0) * 1e9 # slop (hand msgs have rate of ~60hz per hand)

        log = self.get_logger()
        log.info(f"Image topic: {self._image_topic}")
        log.info(f"Hand topic: {self._hand_topic}")
        log.info(f"Object detections topic: {self._obj_det_topic}")
        log.info(f"Device? {self._torch_device}")
        log.info(f"Frames per detection: {self._frames_per_det}")
        log.info(f"Checkpoint: {self._model_checkpoint}")
        log.info(f"Labels: {self._labels_file}")

        # Subscribers for input data channels.
        # These will collect on their own threads, adding to buffers from
        # activity classification will draw from.
        # - Image data
        self._image_subscription = self.create_subscription(
            Image,
            self._image_topic,
            self.image_callback,
            1
        )
        # - Hand pose
        self._hand_subscription = self.create_subscription(
            HandJointPosesUpdate,
            self._hand_topic,
            self.hand_callback,
            1
        )
        # - Object detections
        self._obj_det_subscriber = self.create_subscription(
            ObjectDetection2dSet,
            self._obj_det_topic,
            self.obj_det_callback,
            1
        )

        # Channel over which we communicate to the object detector a timestamp
        # before which to skip processing/publication.
        self._min_time_publisher = self.create_publisher(
            Time,
            self._min_time_topic,
            1
        )
        self._activity_publisher = self.create_publisher(
            ActivityDetection,
            self._det_topic,
            1
        )

        # Create the runtime thread to trigger processing and buffer cleanup
        # appropriately.
        self._input_buffer = InputBuffer(int(self._slop_ns))

        # Stores the data until we have enough to send to the detector
        self._frames = []
        self._hand_poses = dict(
            lhand=[],
            rhand=[],
        )
        self._hand_pose_stamps = dict(
            lhand=[],
            rhand=[],
        )
        self._frame_stamps = []
        self._obj_dets = []

        # Instantiate the activity detector models
        self._detector = get_uho_classifier(
            self._model_checkpoint,
            self._labels_file,
            self._torch_device,
        )
        log.info(f"UHO Detector initialized")
        # TODO: Warmup detector?

        # Start the runtime thread

    def image_callback(self, image: Image) -> None:
        """
        Callback function for images. Messages are saved in the images list.
        """
        self.get_logger().info(f"Queueing image (ts={image.header.stamp})")
        self._input_buffer.queue_image(image)

    def hand_callback(self, hand_pose: HandJointPosesUpdate) -> None:
        """
        Callback function for hand poses. Messages are saved in the hand_poses
        list.
        """
        self.get_logger().info(f"Queueing hand pose (hand={hand_pose.hand}) "
                               f"(ts={hand_pose.header.stamp})")
        self._input_buffer.queue_hand_pose(hand_pose)

    def obj_det_callback(self, msg):
        """
        Callback for the object detection set message. If there are enough frames
        accumulated for the activity detector and there is an object detection
        message received for the last frame in the frame set or after it,
        the activity detector model is called and a new activity detection message
        is published with the current activity predictions.
        """
        log = self.get_logger()

        if msg.num_detections < self._topk:
            log.warn(f"Received msg with less than {self._topk} detections. "
                     f"Skipping.")
            return

        log.info(f"Queueing object detections (ts={msg.header.stamp})")
        self._input_buffer.queue_object_detections(msg)

        # DEBUG: collect like 5 detections and then breakpoint to test calling
        #        the buffer windowing stuff.
        with self._input_buffer:
            window_dets = self._input_buffer.get_window_detections(self._frames_per_det)
            if len(list(filter(None, window_dets))) >= 5:
                win = self._input_buffer.get_window(self._frames_per_det)
                breakpoint()
                # Incrementally clean up to see that resource usage does not ever increase
                clean_ts = time_to_int(win.frames[0][0]) + 1
                log.info(f"Cleaning before ts={clean_ts}")
                self._input_buffer.clear_before(clean_ts)

        # self._obj_dets.append(msg)
        #
        # if len(self._frames) >= self._frames_per_det:
        #     frame_stamp_set = self._frame_stamps[:self._frames_per_det]
        #     ready_to_predict = False
        #
        #     # If the source stamp for this detection message is after or equal to
        #     # the last frame stamp in the current set, then we have all of the
        #     # detections and can move onto processing this set of frames.
        #     msg_nsec = msg.source_stamp.sec * 10e9 + msg.source_stamp.nanosec
        #     frm_nsec = frame_stamp_set[-1].sec * 10e9 + frame_stamp_set[-1].nanosec
        #     if msg_nsec >= frm_nsec:
        #         ready_to_predict = True
        #
        #     # Need to wait until the object detector has processed all of these frames
        #     if not ready_to_predict:
        #         log.info(f"Waiting for more object detection results")
        #         return
        #
        #     # Get the frame synchronized hand poses for this set of frames
        #     frame_set = self._frames[:self._frames_per_det]
        #
        #     lhand_pose_set, rhand_pose_set = get_frame_synced_hand_poses(
        #         frame_stamp_set,
        #         self._hand_poses,
        #         self._hand_pose_stamps,
        #         self._slop_ns
        #     )
        #
        #     # Get the object detections to use
        #     first_frm_nsec = frame_stamp_set[0].sec * 10e9 + frame_stamp_set[0].nanosec
        #     last_frm_nsec = frame_stamp_set[-1].sec * 10e9 + frame_stamp_set[-1].nanosec
        #     obj_det_idxs_to_remove = []
        #     obj_det_set = []
        #     for idx, det in enumerate(self._obj_dets):
        #         if det.num_detections == 0:
        #             log.info(f"no dets, det source: {det.source_stamp}")
        #             continue
        #
        #         det_source_stamp_nsec = det.source_stamp.sec * 10e9 + det.source_stamp.nanosec
        #
        #         # Check that this detection is within the range of time
        #         # for the current frame set
        #         if det_source_stamp_nsec < first_frm_nsec:
        #             # Detection is before the first frame in this set,
        #             # so we can remove it
        #             obj_det_idxs_to_remove.append(idx)
        #             continue
        #         elif det_source_stamp_nsec > last_frm_nsec:
        #             # Detection is after the last frame in this set,
        #             # so keep it for later
        #             continue
        #
        #         obj_det_idxs_to_remove.append(idx)
        #         obj_det_set.append(det)
        #
        #     frame_set_processed, aux_data = create_batch(
        #         frame_set,
        #         lhand_pose_set,
        #         rhand_pose_set,
        #         obj_det_set,
        #         self._topk,
        #     )
        #
        #     # TODO Modularize into angel_system package, use resulting
        #     #      functionality here.
        #     #
        #     # Inference!
        #     #
        #     # Model input format notes from conversations with Dawei.
        #     # - Hand input should include both hands in a flattened vector,p
        #     #   (J*3*2), where Dawei is expecting J=21.
        #     #   shape [32 x (J*3*2)] (J=21 -> 126)
        #     #   - If a one hand is missing from the frame, zero the whole
        #     #     vector out.
        #     # - Detection descriptors and bboxes should not have any zero
        #     #   vectors. There is some expectation of "regularly" spaced
        #     #   detections. A sample-step was described that is set to 5,
        #     #   meaning that the input detections were rigidly assigned to
        #     #   input frames.
        #     #   - new discussion, provided packed matrix, K=<top-k size>
        #     #       [32*K x 2048]
        #     #       [32*K x 4]
        #     activities_detected, labels = self._detector.forward(frame_set_processed, aux_data)
        #
        #     # Create and publish the ActivityDetection msg
        #     activity_msg = ActivityDetection()
        #
        #     # This message time
        #     activity_msg.header.stamp = self.get_clock().now().to_msg()
        #
        #     # Trace to the source
        #     activity_msg.header.frame_id = "Activity detection"
        #     activity_msg.source_stamp_start_frame = frame_stamp_set[0]
        #     activity_msg.source_stamp_end_frame = frame_stamp_set[-1]
        #
        #     activity_msg.label_vec = labels
        #     activity_msg.conf_vec = activities_detected[0].squeeze().tolist()
        #
        #     # Publish!
        #     self._activity_publisher.publish(activity_msg)
        #     log.info(f"Activities detected: {activities_detected}")
        #     log.info(f"Top activity detected: {activities_detected[1]}")
        #
        #     # Clear out stored frames, aux_data, and timestamps
        #     self._frames = self._frames[self._frames_per_det:]
        #     self._frame_stamps = self._frame_stamps[self._frames_per_det:]
        #
        #     # Remove old hand poses
        #     hands = self._hand_pose_stamps.keys()
        #     last_frm_nsec = frame_stamp_set[-1].sec * 10e9 + frame_stamp_set[-1].nanosec
        #     for h in hands:
        #         hand_idxs_to_remove = []
        #         for idx, stamp in enumerate(self._hand_pose_stamps[h]):
        #             h_nsec = stamp.sec * 10e9 + stamp.nanosec
        #             if h_nsec <= last_frm_nsec:
        #                 # Hand pose is before or equal to the last frame
        #                 # in the set of frames we just processed, so we can
        #                 # remove it.
        #                 hand_idxs_to_remove.append(idx)
        #
        #         for i in sorted(hand_idxs_to_remove, reverse=True):
        #             del self._hand_poses[h][i]
        #             del self._hand_pose_stamps[h][i]
        #
        #     for i in sorted(obj_det_idxs_to_remove, reverse=True):
        #         del self._obj_dets[i]

    def thread_predict_runtime(self):
        """
        Activity classification prediction runtime function.
        """
        while True:
            ...


def main():
    rclpy.init()

    detector = UHOActivityDetector()

    rclpy.spin(detector)

    # Destroy the node explicitly
    # (optional - otherwise it will be done automatically
    # when the garbage collector destroys the node object)
    detector.destroy_node()

    rclpy.shutdown()
    cv2.destroyAllWindows()


if __name__ == '__main__':
    main()
