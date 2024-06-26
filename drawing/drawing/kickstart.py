"""
    Kickstart includes a node and service that handles setting up the board for \
    the hangman game.This includes calibrating the robot to the board, drawing \
    the 5 lines for the word to guess, drawing the 5 lines for the 5 wrong \
    guesses, and drawing the stand for the hangman.

Parameters
----------
mode, position

Services:
--------
kickstart_service

Clients For:
---------------
calibrate, where_to_write, moveit_mp, cartesian_mp

"""

import rclpy
from rclpy.node import Node
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup

from std_srvs.srv import Empty

from brain_interfaces.srv import BoardTiles, MovePose, Cartesian


class Kickstart(Node):
    """The kickstart node sets up the hangman game."""

    def __init__(self):
        super().__init__("kickstart")

        # create kickstart service
        self.kickstart_service = self.create_service(
            Empty, 'kickstart_service', self.kickstart_callback)

        # create mutually exclusive callback groups
        self.cal_callback_group = MutuallyExclusiveCallbackGroup()
        self.tile_callback_group = MutuallyExclusiveCallbackGroup()
        self.mp_callback_group = MutuallyExclusiveCallbackGroup()
        self.cartesian_callback_group = MutuallyExclusiveCallbackGroup()

        # create service clients
        self.cal_client = self.create_client(
            Empty, 'calibrate', callback_group=self.cal_callback_group)
        self.tile_client = self.create_client(
            BoardTiles, 'where_to_write',
            callback_group=self.tile_callback_group)
        self.movemp_client = self.create_client(
            MovePose, '/moveit_mp', callback_group=self.mp_callback_group)
        self.cartesian_client = self.create_client(
            Cartesian, '/cartesian_mp', callback_group=self.cartesian_callback_group)

        # wait for clients' services to be available
        while not self.cal_client.wait_for_service(timeout_sec=1.0):
            self.get_logger().info('Calibrate service not available,\
                                   waiting...')
        while not self.tile_client.wait_for_service(timeout_sec=1.0):
            self.get_logger().info('Where to Write service not available,\
                                   waiting...')
        while not self.movemp_client.wait_for_service(timeout_sec=1.0):
            self.get_logger().info('Move It MP service not available,\
                                   waiting...')
        while not self.cartesian_client.wait_for_service(timeout_sec=1.0):
            self.get_logger().info('Carisiam mp  service not available, waiting...')

    async def kickstart_callback(self, request, response):
        """Queuing each component to be drawn for game setup."""
        # Calibrate once
        self.get_logger().error('Calibrating...')
        await self.cal_client.call_async(request=Empty.Request())
        self.get_logger().info('Finished calibrating!')

        # Dashes
        # Draw dashes for word to guess
        await self.draw_component(1, 0)
        self.get_logger().info('Started dashes for word to guess!')
        await self.draw_component(1, 1)
        await self.draw_component(1, 2)
        await self.draw_component(1, 3)
        await self.draw_component(1, 4)

        # Draw dashes for wrong letters
        await self.draw_component(0, 0)
        self.get_logger().info('Started dashes for wrong guesses!')
        await self.draw_component(0, 1)
        await self.draw_component(0, 2)
        await self.draw_component(0, 3)
        await self.draw_component(0, 4)

        # Draw stand for hangman
        self.get_logger().info('Drawing hangman stand!')
        await self.draw_component(3, 0)

        return response

    async def draw_component(self, mode, position):
        """Queues all of the services needed to draw individual\
           components like the stand or dashes."""
        # if mode = 0 or 1 then drawing dashes
        dash_x = [0.01, 0.09, 0.09]
        dash_y = [0.0, 0.0, 0.0]
        dash_on = [True, True, False]

        # if mode = 3 then drawing stand
        stand_x = [-0.01, 0.05, 0.05, 0.05]
        stand_y = [0.05, 0.05, 0.00, 0.00]
        stand_on = [True, True, True, False]

        if mode == 0 or mode == 1:
            # take in mode and position and draw component accordingly
            request = BoardTiles.Request()
            request.mode = mode
            request.position = position
            request.x = dash_x
            request.y = dash_y
            request.onboard = dash_on

            # denote pose_list and initial_pose from BoardTiles response
            resp = await self.tile_client.call_async(request)
            pose1 = resp.initial_pose
            pose_list = resp.pose_list

            # moving to the position
            self.get_logger().info(f"Pose List for Dash: {pose1}")
            self.get_logger().info(f"Pose List for Dash: {pose_list}")
            request2 = Cartesian.Request()
            request2.poses = [pose1]
            request2.velocity = 0.1
            request2.replan = False
            request2.use_force_control = [False]
            await self.cartesian_client.call_async(request2)

            request2 = Cartesian.Request()
            request2.poses = [pose_list[0]]
            request2.velocity = 0.015
            request2.replan = False
            request2.use_force_control = [dash_on[0]]
            await self.cartesian_client.call_async(request2)

            # draw remaining pose dashes with Cartesian mp
            request3 = Cartesian.Request()
            request3.poses = pose_list[1:]
            request3.velocity = 0.015
            request3.replan = True
            request3.use_force_control = dash_on[1:]
            self.get_logger().info(f"pose_list: {pose_list[1:]}")
            await self.cartesian_client.call_async(request3)

        if mode == 3:
            # take in mode and position and draw component accordingly
            request = BoardTiles.Request()
            request.mode = mode
            request.position = position
            request.x = stand_x
            request.y = stand_y
            request.onboard = stand_on

            # denote pose_list and initial_pose from BoardTiles response
            resp = await self.tile_client.call_async(request)
            pose1 = resp.initial_pose
            pose_list = resp.pose_list

            self.get_logger().info(f"Pose List for Stand: {pose1}")
            self.get_logger().info(f"Pose List for Stand: {pose_list}")
            request2 = Cartesian.Request()
            request2.poses = [pose1]
            request2.velocity = 0.1
            request2.replan = False
            request2.use_force_control = [False]
            await self.cartesian_client.call_async(request2)

            request2 = Cartesian.Request()
            request2.poses = [pose_list[0]]
            request2.velocity = 0.015
            request2.replan = False
            request2.use_force_control = [stand_on[0]]
            await self.cartesian_client.call_async(request2)

            # draw remaining pose dashes with Cartesian mp
            request3 = Cartesian.Request()
            request3.poses = pose_list[1:]
            request3.velocity = 0.015
            request3.replan = True
            request3.use_force_control = stand_on[1:]
            self.get_logger().info(f"pose_list: {pose_list[1:]}")
            await self.cartesian_client.call_async(request3)


def main(args=None):
    rclpy.init(args=args)
    node = Kickstart()
    rclpy.spin(node)
    rclpy.shutdown()
