import torch
import math
import genesis as gs
from genesis.utils.geom import quat_to_xyz, transform_by_quat, inv_quat, transform_quat_by_quat

def gs_rand_float(lower, upper, shape, device):
    return (upper - lower) * torch.rand(size=shape, device=device) + lower

class DodoEnv:
    CONTACT_HEIGHT = 0.05           # 低于这个高度视为接触
    SWING_HEIGHT_THRESHOLD = 0.10   # 单脚悬空超过这个高度就视为“hopping”
    def __init__(self,
                 num_envs,
                 env_cfg,
                 obs_cfg,
                 reward_cfg,
                 command_cfg,
                 show_viewer=False):
        # Basic sizes
        self.num_envs = num_envs
        self.num_obs = obs_cfg["num_obs"]
        self.num_actions = env_cfg["num_actions"]
        self.num_commands = command_cfg["num_commands"]
        self.device = gs.device

        # Action latency & timestep
        self.simulate_action_latency = env_cfg.get("simulate_action_latency", True)
        self.dt = 0.01
        self.max_episode_length = math.ceil(env_cfg["episode_length_s"] / self.dt)

        # Store configs
        self.env_cfg = env_cfg
        self.obs_cfg = obs_cfg
        self.reward_cfg = reward_cfg
        self.command_cfg = command_cfg

        # Scales
        self.obs_scales = obs_cfg.get("obs_scales", {})
        self.reward_scales = reward_cfg.get("reward_scales", {})

        # Initialize Genesis scene
        self.scene = gs.Scene(
            sim_options=gs.options.SimOptions(dt=self.dt, substeps=2),
            viewer_options=gs.options.ViewerOptions(
                max_FPS=int(0.5 / self.dt),
                camera_pos=(2.0, 0.0, 2.5),
                camera_lookat=(0.0, 0.0, 0.5),
                camera_fov=40,
            ),
            vis_options=gs.options.VisOptions(rendered_envs_idx=[0]),
            rigid_options=gs.options.RigidOptions(
                dt=self.dt,
                constraint_solver=gs.constraint_solver.Newton,
                enable_collision=True,
                enable_joint_limit=True,
            ),
            show_viewer=show_viewer,
        )

        # Add plane
        self.scene.add_entity(gs.morphs.Plane(fixed=True))

        # Base init
        self.base_init_pos = torch.tensor(env_cfg["base_init_pos"], device=self.device)
        self.base_init_quat = torch.tensor(env_cfg["base_init_quat"], device=self.device)
        self.inv_base_init_quat = inv_quat(self.base_init_quat)

        # Load robot MJCF
        self.robot = self.scene.add_entity(
            gs.morphs.MJCF(
                file="/home/nvidiapc/dodo/Genesis/genesis/assets/urdf/dodo_robot/dodo.xml",
                pos=self.base_init_pos.cpu().numpy(),
                quat=self.base_init_quat.cpu().numpy(),
            )
        )
        self.scene.build(n_envs=num_envs)

        # Joint indices
        self.motors_dof_idx = [
            self.robot.get_joint(name).dof_start
            for name in env_cfg["joint_names"]
        ]
        self.hip_aa_indices = [
            env_cfg["joint_names"].index("Left_HIP_AA"),
            env_cfg["joint_names"].index("Right_HIP_AA"),
        ]
        self.hip_fe_indices = [
            env_cfg["joint_names"].index("Left_THIGH_FE"),
            env_cfg["joint_names"].index("Right_THIGH_FE"),
        ]
        self.knee_fe_indices = [
            env_cfg["joint_names"].index("Left_KNEE_FE"),
            env_cfg["joint_names"].index("Right_SHIN_FE"),
        ]
        self.ankle_links = [
            self.robot.get_link("Right_FOOT_FE"),
            self.robot.get_link("Left_FOOT_FE"),
        ]
        self.hip_angle_history = torch.zeros((self.num_envs, 2, 10), device=self.device)
        self.history_idx = 0


        # PD gains
        kp = [env_cfg["kp"]] * self.num_actions
        kd = [env_cfg["kd"]] * self.num_actions
        self.robot.set_dofs_kp(kp, self.motors_dof_idx)
        self.robot.set_dofs_kv(kd, self.motors_dof_idx)

        # Set action limits
        self.robot.set_dofs_force_range(
            lower=-env_cfg.get("clip_actions", 100.0) * torch.ones(self.num_actions, dtype=torch.float32),
            upper= env_cfg.get("clip_actions", 100.0) * torch.ones(self.num_actions, dtype=torch.float32),
            dofs_idx_local=self.motors_dof_idx,
        )

        # Prepare reward functions
        self.reward_functions = {}
        self.episode_sums = {}
        for name, scale in self.reward_scales.items():
            self.reward_scales[name] = scale * self.dt
            self.reward_functions[name] = getattr(self, f"_reward_{name}")
            self.episode_sums[name] = torch.zeros((self.num_envs,), device=self.device)


        # 假设你的 reward_cfg 里没这两项，就在初始化后手动加上：
        self.reward_scales["foot_contact_penalty"] = -1.0 * self.dt     # 负值 scale，惩罚项
        self.reward_scales["foot_contact_switch"]  = +0.5 * self.dt     # 正值 scale，奖励项

        # 并绑定对应函数
        self.reward_functions["foot_contact_penalty"] = self._reward_foot_contact_penalty
        self.reward_functions["foot_contact_switch"]  = self._reward_foot_contact_switch

        # 为“上一步接触”做缓存
        self.prev_contact = torch.zeros((self.num_envs, 2), device=self.device)  # [num_envs, 左/右]





        # Initialize buffers
        self._init_buffers()

    def _init_buffers(self):
        N, A, C = self.num_envs, self.num_actions, self.num_commands
        self.base_lin_vel = torch.zeros((N, 3), device=self.device)
        self.base_ang_vel = torch.zeros((N, 3), device=self.device)
        self.projected_gravity = torch.zeros((N, 3), device=self.device)
        self.global_gravity = torch.tensor([0.0, 0.0, -1.0], device=self.device).repeat(N, 1)

        self.obs_buf = torch.zeros((N, self.num_obs), device=self.device)
        self.rew_buf = torch.zeros((N,), device=self.device)
        self.reset_buf = torch.ones((N,), device=self.device, dtype=torch.int32)
        self.episode_length_buf = torch.zeros((N,), device=self.device, dtype=torch.int32)

        self.commands = torch.zeros((N, C), device=self.device)
        self.commands_scale = torch.tensor([
            self.obs_scales.get("lin_vel", 1.0),
            self.obs_scales.get("lin_vel", 1.0),
            self.obs_scales.get("ang_vel", 1.0),
        ], device=self.device)

        self.actions = torch.zeros((N, A), device=self.device)
        self.last_actions = torch.zeros_like(self.actions)
        self.dof_pos = torch.zeros_like(self.actions)
        self.dof_vel = torch.zeros_like(self.actions)
        self.last_dof_vel = torch.zeros_like(self.actions)

        self.base_pos = torch.zeros((N, 3), device=self.device)
        self.base_quat = torch.zeros((N, 4), device=self.device)

        self.default_dof_pos = torch.tensor(
            [self.env_cfg["default_joint_angles"][j] for j in self.env_cfg["joint_names"]],
            device=self.device
        )
        self.extras = {"observations": {}}

    def _resample_commands(self, env_ids):
        # Uniformly sample linear and angular velocity targets
        self.commands[env_ids, 0] = gs_rand_float(*self.command_cfg["lin_vel_x_range"], (len(env_ids),), self.device)
        self.commands[env_ids, 1] = gs_rand_float(*self.command_cfg["lin_vel_y_range"], (len(env_ids),), self.device)
        self.commands[env_ids, 2] = gs_rand_float(*self.command_cfg["ang_vel_range"], (len(env_ids),), self.device)

    def reset_idx(self, env_ids):
        if len(env_ids) == 0:
            return
        # Reset joint positions & velocities

        if hasattr(self, "current_ankle_heights"):
            h = self.current_ankle_heights[env_ids]   # 只取这些 env 的高度
        else:
            h = torch.zeros((len(env_ids), 2), device=self.device)
        contact_init = (h < self.CONTACT_HEIGHT).float()
        self.prev_contact[env_ids] = contact_init
        


        self.dof_pos[env_ids] = self.default_dof_pos
        self.dof_vel[env_ids] = 0.0
        self.robot.set_dofs_position(
            position=self.dof_pos[env_ids],
            dofs_idx_local=self.motors_dof_idx,
            zero_velocity=True,
            envs_idx=env_ids
        )
        # Reset base pose & vel
        self.base_pos[env_ids] = self.base_init_pos
        self.base_quat[env_ids] = self.base_init_quat.reshape(1,4)
        self.robot.set_pos(self.base_pos[env_ids], zero_velocity=False, envs_idx=env_ids)
        self.robot.set_quat(self.base_quat[env_ids], zero_velocity=False, envs_idx=env_ids)
        self.base_lin_vel[env_ids] = 0
        self.base_ang_vel[env_ids] = 0
        self.robot.zero_all_dofs_velocity(envs_idx=env_ids)

        # Buffers
        self.last_actions[env_ids] = 0
        self.last_dof_vel[env_ids] = 0
        self.episode_length_buf[env_ids] = 0
        self.reset_buf[env_ids] = True

        # Log episode rewards
        self.extras["episode"] = {}
        for name in self.episode_sums:
            avg = torch.mean(self.episode_sums[name][env_ids]).item() / self.env_cfg["episode_length_s"]
            self.extras["episode"][f"rew_{name}"] = avg
            self.episode_sums[name][env_ids] = 0.0

        # New commands
        self._resample_commands(env_ids)

    def reset(self):
        self.reset_buf[:] = 1
        self.reset_idx(torch.arange(self.num_envs, device=self.device))
        return self.obs_buf, None

    def step(self, actions):
        # Clip actions
        self.actions = torch.clip(actions, -self.env_cfg["clip_actions"], self.env_cfg["clip_actions"])
        # Latency
        exec_actions = self.last_actions if self.simulate_action_latency else self.actions
        # Position target
        target_pos = exec_actions * self.env_cfg["action_scale"] + self.default_dof_pos
        self.robot.control_dofs_position(target_pos, self.motors_dof_idx)
        
        # Step sim
        self.scene.step()
        heights = []
        foot_orientations = []
        for link in self.ankle_links:
            pos = link.get_pos()        # shape: (num_envs, 3)
            heights.append(pos[:, 2])
            foot_quat = link.get_quat()  # shape: (num_envs, 4)
            foot_orientations.append(foot_quat)
        
        self.current_ankle_heights = torch.stack(heights, dim=1)
        self.current_foot_orientations = torch.stack(foot_orientations, dim=1)  # shape: (num_envs, 2, 4)

        # Time and pose updates
        self.episode_length_buf += 1
        self.base_pos[:] = self.robot.get_pos()
        self.base_quat[:] = self.robot.get_quat()
        # Euler angles in degrees
        base_rel_quat = transform_quat_by_quat(torch.ones_like(self.base_quat)*self.inv_base_init_quat, self.base_quat)
        self.base_euler = quat_to_xyz(base_rel_quat, rpy=True, degrees=True)
        # Velocities in base frame
        inv_q = inv_quat(self.base_quat)
        self.base_lin_vel[:] = transform_by_quat(self.robot.get_vel(), inv_q)
        self.base_ang_vel[:] = transform_by_quat(self.robot.get_ang(), inv_q)
        self.projected_gravity[:]= transform_by_quat(self.global_gravity, inv_q)
        self.dof_pos[:] = self.robot.get_dofs_position(self.motors_dof_idx)
        self.dof_vel[:] = self.robot.get_dofs_velocity(self.motors_dof_idx)

        current_hip_angles = torch.stack([
            self.dof_pos[:, self.hip_fe_indices[0]],
            self.dof_pos[:, self.hip_fe_indices[1]]
        ], dim=1)

        self.hip_angle_history[:, :, self.history_idx] = current_hip_angles
        self.history_idx = (self.history_idx + 1) % 10

        # Resample commands
        idx = (self.episode_length_buf % int(self.env_cfg["resampling_time_s"] / self.dt)==0).nonzero(as_tuple=False).flatten()
        self._resample_commands(idx)

        # Terminate if out of bounds or timeout
        done = self.episode_length_buf > self.max_episode_length
        done |= torch.abs(self.base_euler[:,1]) > self.env_cfg["termination_if_pitch_greater_than"]
        done |= torch.abs(self.base_euler[:,0]) > self.env_cfg["termination_if_roll_greater_than"]
        self.reset_buf = done

        # Reward computation
        self.rew_buf[:] = 0
        for name, fn in self.reward_functions.items():
            r = fn() * self.reward_scales[name]
            self.rew_buf += r
            self.episode_sums[name] += r

        # Observation assembly
        self.obs_buf = torch.cat([
            self.base_ang_vel * self.obs_scales["ang_vel"],
            self.projected_gravity,
            self.commands * self.commands_scale,
            (self.dof_pos - self.default_dof_pos)*self.obs_scales["dof_pos"],
            self.dof_vel * self.obs_scales["dof_vel"],
            self.actions
        ], dim=-1)

        self.last_actions[:] = self.actions
        self.last_dof_vel[:] = self.dof_vel

        self.extras["observations"]["critic"] = self.obs_buf
        # Reset environments
        self.reset_idx(self.reset_buf.nonzero(as_tuple=False).flatten())
        return self.obs_buf, self.rew_buf, self.reset_buf, self.extras
    
    def get_observations(self):
        """
        Returns the current observation buffer and extras dict.
        This is called once at the start by OnPolicyRunner.
        """
        # ensure extras['observations']['critic'] is up to date
        self.extras["observations"]["critic"] = self.obs_buf
        return self.obs_buf, self.extras

    def get_privileged_observations(self):
        """
        If you have any privileged state (e.g. ground-truth sim info), return it here.
        Otherwise just return None.
        """
        return None

    # Reward functions
    def _reward_tracking_lin_vel(self):
        err = torch.sum((self.commands[:,:2] - self.base_lin_vel[:,:2])**2, dim=1)
        return torch.exp(-err / self.reward_cfg["tracking_sigma"])

    def _reward_tracking_ang_vel(self):
        err = (self.commands[:,2] - self.base_ang_vel[:,2])**2
        return torch.exp(-err / self.reward_cfg["tracking_sigma"])

    def _reward_lin_vel_z(self):
        return self.base_lin_vel[:,2]**2

    def _reward_action_rate(self):
        return torch.sum((self.last_actions - self.actions)**2, dim=1)

    def _reward_similar_to_default(self):
        return torch.sum(torch.abs(self.dof_pos - self.default_dof_pos), dim=1)

    def _reward_base_height(self):
        return (self.base_pos[:,2] - self.reward_cfg["base_height_target"])**2

    def _reward_orientation_stability(self):
        pr = torch.abs(self.base_euler[:,1] * math.pi/180)
        rr = torch.abs(self.base_euler[:,0] * math.pi/180)
        return pr**2 + rr**2

    def _reward_survive(self):
        return torch.ones(self.num_envs, device=self.device)
    
    def _reward_penalize_hip_aa(self):
        # self.dof_pos 的 shape 是 [num_envs, num_actions]

        return torch.sum(torch.abs(self.dof_pos[:, self.hip_aa_indices]), dim=1)
    def _reward_penalize_hip_fe(self):
        # self.dof_pos 的 shape 是 [num_envs, num_actions]

        return torch.sum(torch.abs(self.dof_pos[:, self.hip_fe_indices]), dim=1)
    def _reward_penalize_hip_fe_diff(self):
        # peanlize the difference between left and right hip fe absolute value
        return torch.abs(self.dof_pos[:, self.hip_fe_indices[0]] - self.dof_pos[:, self.hip_fe_indices[1]])
    # def _reward_penalize_knee_fe(self):
    #     # Penalize Right_SHIN_FE if > +0.9, and Left_KNEE_FE if < -0.9. No penalty otherwise.
    #     # self.dof_pos shape: [num_envs, num_actions]
    #     pos = self.dof_pos[:, self.knee_fe_indices]
    #     left_knee = pos[:, 0]   # Left_KNEE_FE
    #     right_shin = pos[:, 1]  # Right_SHIN_FE

    #     # For Left_KNEE_FE, penalize if below -0.9: deviation = relu(-0.9 - angle)
    #     left_dev = torch.relu(0.9 + left_knee)
    #     # For Right_SHIN_FE, penalize if above +0.9: deviation = relu(angle - 0.9)
    #     right_dev = torch.relu(-right_shin+0.9)

    #     return left_dev + right_dev
    
    def _reward_penalize_knee_fe_left(self):
        pos = self.dof_pos[:, self.knee_fe_indices]
        left_knee = pos[:, 0]   # Left_KNEE_FE
        left_dev = torch.relu(0.9 + left_knee)
        return left_dev
    def _reward_penalize_knee_fe_right(self):
        pos = self.dof_pos[:, self.knee_fe_indices]
        right_shin = pos[:, 1]
        right_dev = torch.relu(-right_shin + 0.9)
        return right_dev


    def _reward_penalize_ankle_height(self):
        return torch.mean(self.current_ankle_heights, dim=1)
    
    def _reward_gait_regularity(self):

        left_hip = self.dof_pos[:, self.hip_fe_indices[0]]   
        right_hip = self.dof_pos[:, self.hip_fe_indices[1]]  
        
        phase_diff = torch.abs(left_hip + right_hip) 
        phase_reward = torch.exp(-phase_diff / 0.3)   
        
        if torch.sum(self.hip_angle_history) != 0: 
            left_hip_history = self.hip_angle_history[:, 0, :]  # (num_envs, 10)
            
            hip_changes = torch.diff(left_hip_history, dim=1)  # (num_envs, 9)
            
            change_consistency = torch.zeros(self.num_envs, device=self.device)
            for i in range(hip_changes.shape[1] - 1):

                direction_consistency = torch.sign(hip_changes[:, i]) * torch.sign(hip_changes[:, i+1])
                change_consistency += torch.clamp(direction_consistency, min=0)
            
            change_consistency = change_consistency / (hip_changes.shape[1] - 1)
            periodicity_reward = change_consistency * 0.5  
        else:
            periodicity_reward = torch.zeros(self.num_envs, device=self.device)
        
        total_gait_reward = phase_reward + periodicity_reward
        
        return total_gait_reward

    def _reward_foot_orientation(self):
        """
        Reward function for foot orientation with two components:
        1. Individual foot orientation (rewards flat feet, penalizes tilted feet)
        2. Foot orientation consistency (penalizes different orientations between feet)
        """
        orientation_rewards = []
        
        # Use -z axis as sole normal (update based on your debug output)
        sole_normal_local = torch.tensor([0., 0., -1.], device=self.device)
        world_down = torch.tensor([0., 0., -1.], device=self.device)
        
        foot_alignments = []  # Store individual foot alignments for consistency check
        
        for i in range(len(self.ankle_links)):
            foot_quat = self.current_foot_orientations[:, i, :]  # (num_envs, 4)
            
            # Transform sole normal to world coordinates
            world_sole_normal = transform_by_quat(
                sole_normal_local.expand(self.num_envs, 3), 
                foot_quat
            )
            
            # Calculate alignment with world down direction
            # REMOVED clamp(min=0.0) to allow negative values (penalties)
            alignment = torch.sum(world_sole_normal * world_down.expand_as(world_sole_normal), dim=1)
            
            orientation_rewards.append(alignment)
            foot_alignments.append(alignment)
        
        # Component 1: Mean orientation reward (can be negative for penalty)
        mean_orientation_reward = torch.mean(torch.stack(orientation_rewards, dim=1), dim=1)
        
        # Component 2: Foot orientation consistency penalty
        # Penalize when the two feet have different orientations
        if len(foot_alignments) >= 2:
            left_foot_alignment = foot_alignments[0]   # First foot
            right_foot_alignment = foot_alignments[1]  # Second foot
            
            # Calculate absolute difference in orientations
            orientation_diff = torch.abs(left_foot_alignment - right_foot_alignment)
            
            # Convert to penalty (negative reward for differences)
            consistency_penalty = -orientation_diff
            
            # Combine: 0.5 * orientation_reward + 0.5 * consistency_penalty
            total_reward = 0.5 * mean_orientation_reward + 0.05 * consistency_penalty
        else:
            # If only one foot, just use orientation reward
            total_reward = mean_orientation_reward
        
        return total_reward
        
    def _reward_step_height_consistency(self):

        left_height = self.current_ankle_heights[:, 1]   
        right_height = self.current_ankle_heights[:, 0]  
        
        height_diff = torch.abs(left_height - right_height)
        
        consistency_reward = torch.exp(-height_diff / 0.05) 
        
        return consistency_reward
    
    def _reward_foot_contact_penalty(self):
        """
        惩罚项：当出现单腿承重且摆动腿抬得太高，或者出现双脚都不着地的“飞行”阶段时给出 penalty。
        """
        h = self.current_ankle_heights      # [N, 2]
        contact = (h < self.CONTACT_HEIGHT).float()   # [N,2] 0/1

        # 1) 双脚都不接触（飞行阶段）：contact.sum==0
        flight = (contact.sum(dim=1) == 0).float()

        # 2) 单脚承重且摆动腿抬得太高：contact.sum==1 且 max_height > SWING_HEIGHT_THRESHOLD
        one_contact = (contact.sum(dim=1) == 1).float()
        max_swing_h = torch.max(h, dim=1)[0]  # 取两只脚最高高度
        swing_hopping = one_contact * torch.relu(max_swing_h - self.SWING_HEIGHT_THRESHOLD)

        return flight + swing_hopping


    def _reward_foot_contact_switch(self):
        """
        奖励项：鼓励左右脚交替着地（contact 状态翻转）。
        只要本步左右脚同时发生翻转，就给 reward=1，否则 0。
        """
        h = self.current_ankle_heights
        contact = (h < self.CONTACT_HEIGHT).float()  # [N,2]

        # 本步每条腿的 contact 状态是否跟上一步不同
        change = (contact != self.prev_contact).float()  # [N,2]

        # 只有当左右都翻转（contact 状态同时变化）才算一次完整的交替
        both_changed = (change[:,0] * change[:,1])

        # 更新缓存
        self.prev_contact[:] = contact

        return both_changed


