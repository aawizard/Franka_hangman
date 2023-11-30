import rclpy
from rclpy.node import Node

from std_srvs.srv import Empty
from geometry_msgs.msg import Point, Quaternion, Pose
from sensor_msgs.msg import JointState

from path_planner.path_plan_execute import Path_Plan_Execute
from character_interfaces.alphabet import alphabet
from joint_interfaces.msg import JointTrajectories

from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from enum import Enum, auto

from action_msgs.msg import GoalStatus

from std_msgs.msg import String, Float32

from tf2_ros.buffer import Buffer
from tf2_ros.transform_listener import TransformListener

import tf2_ros
from brain_interfaces.srv import MovePose, MoveJointState, Cartesian
from brain_interfaces.msg import EEForce

import numpy as np
import transforms3d as tf
np.set_printoptions(suppress=True)


class State(Enum):

    TOUCHING_BOARD = auto()
    WRITING_LETTERS = auto()
    CALIBRATE = auto()
    GET_TRANSFORM = auto()
    LOAD_MOVES = auto()
    STOP = auto()
    PLANNING = auto()
    EXECUTING = auto()
    WAITING = auto()

    PLAN_MOVEGROUP = auto()
    PLAN_CARTESIAN_MOVE = auto()


class Drawing(Node):
    """
    Pick up trash with the Franka.

    Drive a robot around to pickup objects that are a user specified
    distance within its workspace.

    Args:
    ----
    None

    """

    def __init__(self):

        super().__init__("Drawing")

        # declare parameters
        self.declare_parameter('use_fake_hardware', True)
        self.declare_parameter('x_init', 0.5)
        self.declare_parameter('y_init', 0.0)
        self.declare_parameter('robot_name', 'panda')
        self.declare_parameter('group_name', 'panda_manipulator')
        self.declare_parameter('frame_id', 'panda_link0')

        # get parameters
        self.use_fake_hardware = self.get_parameter(
            'use_fake_hardware').get_parameter_value().bool_value

        # if self.use_fake_hardware == "true":
        #     self.use_fake_hardware = True
        # else:
        #     self.use_fake_hardware = False

        self.x_init = self.get_parameter(
            'x_init').get_parameter_value().double_value
        self.y_init = self.get_parameter(
            'y_init').get_parameter_value().double_value

        self.robot_name = self.get_parameter(
            'robot_name').get_parameter_value().string_value
        self.group_name = self.get_parameter(
            'group_name').get_parameter_value().string_value
        self.frame_id = self.get_parameter(
            'frame_id').get_parameter_value().string_value

        # Initialize variables
        self.joint_names = []
        self.joint_pos = []
        self.timer_callback_group = MutuallyExclusiveCallbackGroup()
        self.moveit_mp_callback_group = MutuallyExclusiveCallbackGroup()
        self.cartesian_mp_callback_group = MutuallyExclusiveCallbackGroup()
        self.jointstate_mp_callback_group = MutuallyExclusiveCallbackGroup()
        self.execute_trajectory_status_callback_group = MutuallyExclusiveCallbackGroup()
        self.timer = self.create_timer(
            0.01, self.timer_callback, callback_group=self.timer_callback_group)

        self.path_planner = Path_Plan_Execute(self)

        # these are used for computing the current location of the end-effector
        # using the tf tree.
        self.buffer = Buffer()
        self.listener = TransformListener(self.buffer, self)

        ##### create services #####
        # this service is for the brain node to send singular poses for
        # this node to plan paths using te moveite motion planner
        self.moveit_mp_service = self.create_service(
            MovePose, '/moveit_mp', self.moveit_mp_callback, callback_group=self.moveit_mp_callback_group)

        # this service is for the brain node to send lists of poses for
        # this node to use to plan paths using the cartesian motion
        # planner. It also needs a Point() object, which contains the
        # start position of the letter to be planned.
        self.cartesian_mp_service = self.create_service(
            Cartesian, '/cartesian_mp', self.cartesian_mp_callback, callback_group=self.cartesian_mp_callback_group)

        # this service is for other ROS nodes to send a JointState() msg
        # to this node. This node will plan a path to the combination of
        # joint states and the move there.
        self.plan_joint_state_service = self.create_service(
            MoveJointState, '/jointstate_mp', self.jointstate_mp_callback, callback_group=self.jointstate_mp_callback_group)

        ############# create subscribers ################

        # this subscriber is used for communicating with the node we created
        # to execute our trajectories.
        self.execute_trajectory_status_sub = self.create_subscription(
            String, '/execute_trajectory_status', self.execute_trajectory_status_callback, 10, callback_group=self.execute_trajectory_status_callback_group)

        ############# create publishers ##############

        # this publisher is used to send the joint trajectories we plan to our
        # node that we created to execute them.
        self.joint_traj_pub = self.create_publisher(
            JointTrajectories, '/joint_trajectories', 10)

        # this publisher is used to send the current force at the end-effector
        # to the node we created to execute trajectories.
        self.force_pub = self.create_publisher(
            EEForce, '/ee_force', 10)

        self.font_size = 0.1

        self.moveit_mp_queue = []  # moveit motion planner queue
        self.cartesian_mp_queue = []  # cartesian motion planner queue
        self.letter_start_point = []

        self.state = State.CALIBRATE

        self.L1 = 0.1070  # length of panda_link7
        self.L2 = 0.1130  # distancefrom panda_joint7 to gripper tips

        self.gripper_mass = 1.795750991  # kg
        self.g = 9.81  # m/s**2

        # position of the center of mass of the end-effector
        # in the panda_hand frame
        self.pc = np.array([-0.01, 0, 0.03])
        # position of the tip of the end-effector in the panda_hand frame
        self.pe = np.array([0, 0, 0.1034])
        self.p6f = np.array([0.088, -0.1070, 0])

        self.force_offset = 0.0  # N
        self.force_threshold = 3.0  # N
        self.calibration_counter = 0.0  # N
        self.ee_force = 0.0  # N
        self.use_force_control = False

        self.current_pos = Point(x=0.0, y=0.0, z=0.0)
        self.letter_start_pos = Point(x=0.0, y=0.0, z=0.0)

        self.home_position = Pose(
            position=Point(x=-0.5, y=0.0, z=0.4),
            orientation=Quaternion(x=1.0, y=0.0, z=0.0, w=0.0)
        )

        self.prev_state = State.STOP

    def array_to_transform_matrix(self, translation, quaternion):
        # Normalize the quaternion
        quaternion /= np.linalg.norm(quaternion)
        quaternion = [quaternion[3], quaternion[0],
                      quaternion[1], quaternion[2]]

        # Create rotation matrix from quaternion
        rotation_matrix = tf.quaternions.quat2mat(quaternion)

        # Create the transformation matrix
        transform_matrix = np.eye(4)
        transform_matrix[:3, :3] = rotation_matrix
        transform_matrix[:3, 3] = translation

        return transform_matrix, rotation_matrix

    def calc_joint_torque_offset(self):

        pw6, quaternion_w6 = self.get_transform(
            'panda_link0', 'panda_link6')

        Tw6, Rw6 = self.array_to_transform_matrix(pw6, quaternion_w6)

        p6f, quaternion_6f = self.get_transform('panda_link6', 'panda_hand')

        # self.get_logger().info(f"p6f: {p6f}")
        # self.get_logger().info(f"quaternioon_6f: {quaternion_6f}")

        T6f, R6f = self.array_to_transform_matrix(p6f, quaternion_6f)

        # self.get_logger().info(f"T6f: {T6f}")

        Fw = np.array([0, 0, -self.gripper_mass * self.g])
        F6 = np.linalg.inv(Rw6) @ Fw
        M6 = F6 * (p6f + R6f @ self.pc)
        # M6 = np.array([F6[0] * p6f[2]])

        # self.get_logger().info(f"R6f @ self.pc: {R6f @ self.pc}")
        # self.get_logger().info(f"p6f: {p6f + R6f @ self.pc}")
        # self.get_logger().info(f"Rw6: {np.linalg.inv(Rw6)}")
        # self.get_logger().info(f"Fw: {Fw}")
        # self.get_logger().info(f"Rw6 @ Fw: {np.linalg.inv(Rw6) @ Fw}")
        # self.get_logger().info(f"F6: {F6}")
        # self.get_logger().info(f"M6: {M6}")

        joint_torque_offset = M6[1]

        return joint_torque_offset

    def calc_ee_force(self, effort_joint6):

        pe6, quaternion_e6 = self.get_transform(
            'panda_hand_tcp', 'panda_link6')

        Te6, Re6 = self.array_to_transform_matrix(pe6, quaternion_e6)

        p6e, quaternion_6e = self.get_transform(
            'panda_link6', 'panda_hand_tcp')

        T6e, R6e = self.array_to_transform_matrix(p6e, quaternion_6e)

        M6 = np.array([0, effort_joint6, 0])
        F6 = np.divide(M6, p6e,
                       out=np.zeros_like(p6e), where=p6e != 0)

        Fe = Re6 @ F6

        return Fe

    def execute_trajectory_status_callback(self, msg):

        # the "done" message signifies that the trajectory execution node has finished
        # executing the trajectory it was assigned. Once this happens, we should go back
        # to the planning state, and from there if there's nothing in the queue the state
        # will change to waiting.

        if msg.data == "done":
            trans, rotation = self.get_transform(
                'panda_link0', 'panda_hand_tcp')
            self.letter_start_pos = Point(x=trans[0], y=trans[1], z=trans[2])
            self.state = State.PLAN_MOVEGROUP

    async def moveit_mp_callback(self, request, response):

        self.get_logger().info(f"MOVEIT MOTION PLAN REQUEST RECEIVED")

        self.moveit_mp_queue.append(request.target_pose)
        self.state = State.PLAN_MOVEGROUP
        self.use_force_control = False

        return response

    def cartesian_mp_callback(self, request, response):
        '''
        Queue a letter to be drawn.

        This function will be called when the brain node sends
        this node a message with a cartesian path to plan. I had an
        idea that the brain can just send this node a list of poses
        that would need to traveled to for drawing a letter, and that
        this node could add in some in-between movements that help guide
        the robot to the correct position on the board. We can discuss this.

        Args:
        ----
        msg: the custom message (brain_interfaces/Cartesian.msg)
        '''

        self.get_logger().info(f"CARTESIAN MOTION PLAN REQUEST RECEIVED")

        # self.letter_start_point.y = request.start_point.y
        # self.letter_start_point.z = request.start_point.z

        self.cartesian_mp_queue += request.poses
        self.state = State.PLAN_CARTESIAN_MOVE
        self.use_force_control = True

        return response

    def jointstate_mp_callback(self, request, response):
        '''
        Queue a JointState to be planned for.

        When the jointstate_mp service is called, we set the goal_joint_state 
        to the joint_state from the service call directly, instead of
        using a Pose() message with the compute_ik service like the moveit 
        motion planner. Once this JointState is planned for, it will be 
        immediately executed.

        Args:
        ----
        request: A JointState() message we want to plan a path to.
        response: An empty message.
        '''

        self.get_logger().info(f"JOINTSTATE MOTION PLAN REQUEST RECEIVED")

        joints_to_move = list(
            zip(request.joint_names, request.joint_positions))
        # N = len(joints_to_move)
        self.path_planner.goal_joint_state = self.path_planner.current_joint_state
        self.path_planner.goal_joint_state.effort = []  # haha!!
        self.path_planner.goal_joint_state.header.stamp.nanosec = 0
        self.path_planner.goal_joint_state.header.stamp.sec = 0
        self.path_planner.goal_joint_state.header.frame_id = 'panda_link0'

        # while joints_to_move:
        self.get_logger().info(
            f"goal_joint_tstae_names: {self.path_planner.goal_joint_state.name}")
        for i in range(len(self.path_planner.goal_joint_state.name)-2):
            # self.get_logger().info(f'{ self.path_planner.goal_joint_state.name[i]} 1')ointTrajectory(head
            # self.get_logger().info(f'{joints_to_move[0]} 2')
            if len(joints_to_move) > 0:
                if joints_to_move[0][0] == self.path_planner.goal_joint_state.name[i]:
                    self.path_planner.goal_joint_state.position[i] = joints_to_move[0][1]
                    joints_to_move.pop(0)

        self.get_logger().info(
            f"goal_jiont_staet: {self.path_planner.goal_joint_state}")

        self.path_planner.plan_path()

        self.state = State.WAITING

        return response

    def get_transform(self, parent_frame, child_frame):
        """
        Try catch block for listening to transforms between parent and child frame.

        Args:
        ----
        parent_frame (string): name of parent frame
        child_frame (string): name of child frame

        Returns
        -------
        brick_to_platform: the x,y,z of the translational transform

        """
        try:
            trans = self.buffer.lookup_transform(
                parent_frame, child_frame, rclpy.time.Time()
            )
            transl = trans.transform.translation
            rot = trans.transform.rotation
            brick_to_platform = np.array([transl.x, transl.y, transl.z])
            rotation = np.array([rot.x, rot.y, rot.z, rot.w])

            # print(brick_to_platform[2])
            return brick_to_platform, rotation

        except tf2_ros.LookupException as e:
            # the frames don't exist yet
            self.get_logger().info(f"Lookup exception: {e}")
            return [0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]
        except tf2_ros.ConnectivityException as e:
            # the tf tree has a disconnection
            self.get_logger().info(f"Connectivity exception: {e}")
            return [0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]
        except tf2_ros.ExtrapolationException as e:
            # the times are two far apart to extrapolate
            self.get_logger().info(f"Extrapolation exception: {e}")
            return [0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]

    async def timer_callback(self):
        """
        Timer loop for the drawing node.

        The timer loop functions as the main loop of the node, and
        also contains a state machine. If the gripper server is
        available, the state machine will contain actions for the
        gripper. If it is not available, as is the case when using
        fake hardware, the gripper will not be used.

        Args:
        ----
        None

        """
        if self.state == State.CALIBRATE:

            # here we figure out what the force offset should be by using an average.
            # we take 100 readings of the effort in panda_joint6, and take the average
            # to assign the force offset in the joint due to gravity.
            self.get_logger().info(
                f"self.use_fake_hardware: {self.use_fake_hardware}")
            if self.use_fake_hardware:
                self.state = State.WAITING
                return

            while not self.path_planner.current_joint_state.effort:
                return

            calibration_cycles = 100
            while self.calibration_counter < calibration_cycles:
                self.force_offset += self.path_planner.current_joint_state.effort[5] / (
                    self.L1 + self.L2)
                self.calibration_counter += 1

            self.force_offset = self.force_offset/calibration_cycles
            self.get_logger().info(
                f"force offset complete: {self.force_offset}")
            self.state = State.WAITING

        elif self.state == State.PLAN_MOVEGROUP:

            # here we check to see if the big_move queue is empty, and if not,
            # we use the moveit motion planner to create a trajectory.
            # then we go to the waiting loop, where we will wait for the future
            # to return true.

            if not self.moveit_mp_queue:  # check if the queue is empty
                self.state == State.PLAN_CARTESIAN_MOVE
                return

            self.get_logger().info(
                f"self.moveit_mp_queue[0]: {self.moveit_mp_queue[0]}")
            await self.path_planner.get_goal_joint_states(self.moveit_mp_queue[0])
            self.get_logger().info("here")
            self.path_planner.plan_path()

            self.state = State.WAITING

            self.moveit_mp_queue.pop(0)

        elif self.state == State.PLAN_CARTESIAN_MOVE:

            # check to see if the cartesian move queue is empty, and if not
            # then plan a cartesian path using the poses in the queue. The
            # /compute_cartesian_path service takes in a list of poses, and
            # creates a trajectory to visit all of those poses.

            if not self.cartesian_mp_queue:
                self.state == State.WAITING

            self.get_logger().info(f"queue: {self.cartesian_mp_queue}")

            await self.path_planner.plan_cartesian_path(self.cartesian_mp_queue)

            self.cartesian_mp_queue.clear()
            self.state = State.EXECUTING

        elif self.state == State.EXECUTING:

            # send the trajectory previously planned, either by the moveit motion
            # planner or the cartesian path planner, to our node for executing trajectories.

            joint_trajectories = JointTrajectories()
            joint_trajectories.clear = False
            joint_trajectories.state = "publish"

            joint_trajectories.joint_trajectories = self.path_planner.execute_individual_trajectories()

            self.get_logger().info(
                f"current joint states: {self.path_planner.current_joint_state.position}")
            self.joint_traj_pub.publish(joint_trajectories)

            self.state = State.WAITING

        elif self.state == State.WAITING:

            # calculate the current force at the end-effector, and send it to the
            # node that is executing our trajectory. Also, check to see whether
            # the moveit motion planner has completed planning. This will only
            # happen if the state prior was State.PLAN_MOVEGROUP.

            joint_torque_offset = self.calc_joint_torque_offset()

            self.ee_force = self.calc_ee_force(
                self.path_planner.current_joint_state.effort[5] - joint_torque_offset)

            ee_force_msg = EEForce()
            ee_force_msg.ee_force = self.ee_force[2]
            ee_force_msg.use_force_control = self.use_force_control

            self.force_pub.publish(ee_force_msg)

            if self.path_planner.movegroup_status == GoalStatus.STATUS_SUCCEEDED:

                self.state = State.EXECUTING
                self.path_planner.movegroup_status = GoalStatus.STATUS_UNKNOWN


def main(args=None):
    rclpy.init(args=args)

    drawing = Drawing()

    rclpy.spin(drawing)


if __name__ == '__main__':
    main()
