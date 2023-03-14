from cv_bridge import CvBridge
from rclpy.node import Node
from sensor_msgs.msg import Image
import rclpy
import cv2
import threading

from angel_utils import RateTracker


BRIDGE = CvBridge()


class ImagePublisher(Node):
    """
    Publish a static image at a constant rate.
    """

    def __init__(self):
        super().__init__(self.__class__.__name__)
        self._image_topic = (
            self.declare_parameter("image_topic", "debug/FramesBGR")
            .get_parameter_value()
            .string_value
        )
        self._im_to_show = (
            self.declare_parameter("_im_to_show", "/angel_workspace/model_files/test_images/0c7546f9-frame_04331_01661883024_582920313.png")
            .get_parameter_value()
            .string_value
        )
        self._pub_rate = float(
            self.declare_parameter("publish_rate_hz", 1.0).value
        )
        self._rt_window = int(
            self.declare_parameter("rate_tracker_window", 10).value
        )

        self._rt = RateTracker()

        self.logger = self.get_logger()
        self._publisher = self.create_publisher(
            Image,
            self._image_topic,
            1
        )
        self.logger.info("starting")

    def get_pub_rate(self):
        return self._pub_rate

    def publish_image(self):
        msg = BRIDGE.cv2_to_imgmsg(cv2.imread(self._im_to_show), encoding='bgr8')
        self._publisher.publish(msg)
        self._rt.tick()
        self.get_logger().info(f"Publishing static image (hz: "
                               f"{self._rt.get_rate_avg()})",
                               throttle_duration_sec=1)


def main():
    rclpy.init()

    node = ImagePublisher()
    thread = threading.Thread(target=rclpy.spin, args=(node, ), daemon=True)
    thread.start()
    #rclpy.spin(node)
    rate = node.create_rate(node.get_pub_rate())
    while rclpy.ok():
        node.publish_image()
        rate.sleep()
    # Destroy the node explicitly
    # (optional - otherwise it will be done automatically
    # when the garbage collector destroys the node object)
    node.destroy_node()

    rclpy.shutdown()


if __name__ == '__main__':
    main()
