from keras.api.models import Sequential, Model
from keras.api.layers import Dense, Lambda, Conv1D, MaxPool1D, Flatten
from keras.api.models import load_model
import keras.api.backend as K
import keras
from keras import ops
import numpy as np
import tensorflow as tf
from drawing_bot_api.trajectory_optimizer.config import *
from drawing_bot_api.config import PLOT_XLIM, PLOT_YLIM
import os
from drawing_bot_api.trajectory_optimizer.image_processor import ImageProcessor
from drawing_bot_api.logger import Log
from math import sqrt, pow, atan2, pi, tanh
import sys

class LossHistory(keras.callbacks.Callback):
    def __init__(self, type='critic'):
        self.losses = []
        self.buffer = []
        self.call_counter = 0
        self.type = type

    def on_epoch_end(self, epoch, logs={}):
        self.losses.append(logs.get('loss'))

class Scheduler:
    def __init__(self, base_value, gamma):
        self.base_value = base_value
        self.gamma = gamma
        self.call_counter = 0

    def __call__(self, count_up=True):
        _value = self.base_value * pow(self.gamma, self.call_counter)
        if count_up:
            self.call_counter += 1
        return _value

############################################################
# CUSTOM LOSSES ############################################
############################################################

def pass_through_loss(y_true, y_pred):
    #tf.print("y_true:", y_true)
    return ops.mean(ops.abs(y_true * y_pred), axis=-1)

def entropy_loss(y_true, y_pred):
    entropy = -tf.reduce_sum(y_pred * tf.math.log(tf.clip_by_value(y_pred, 1e-10, 1.0)), axis=-1)
    advantage_loss = ops.mean(ops.abs(y_true * y_pred), axis=-1)
    return advantage_loss - 0.01 * entropy  # Add entropy regularization

def actor_loss(y_true, y_pred):
   # y_true contains [actions, advantages]
    actions = y_true[:, :2]  # Extract actions
    advantages = y_true[:, 2:]  # Extract advantages
    advantages = ops.mean(advantages, axis=-1)

    # y_pred is a list of outputs: [means, sigmas]
    means = y_pred[:, :2]
    sigmas = y_pred[:, 2:]
    sigmas = tf.clip_by_value(sigmas, SIGMA_MIN, SIGMA_MAX)
    sigmas_mean = ops.mean(sigmas, axis=-1)
    #sigmas = (sigmas * SIGMA_SCALING) + SIGMA_LIMIT_MIN

    # Compute Gaussian log-probabilities
    log_probs = -0.5 * ops.sum(((actions - means) / (sigmas + 1e-8))**2 + 2 * ops.log(sigmas + 1e-8) + ops.log(2 * np.pi), axis=1)
    #tf.print('log probs:', log_probs, summarize=-1)

    # calculate entropies
    action_penalty = ops.average(ops.square(means))
    sigma_entropy = ops.sum(ops.log(sigmas + 1e-8))
    sigma_penalty = ops.average(ops.square(sigmas))
    advantage_penalty = ops.max(ops.stack((-advantages, advantages*sigmas_mean), axis=1), axis=1)

    if False:
        tf.print('log probs', log_probs)
        tf.print('action penalty', action_penalty)
        tf.print('sigma penalty', sigma_penalty)

    # Scale log-probabilities by advantages
    means_loss = ops.mean(-log_probs * advantages) + ACTION_PENALTY_FACTOR * action_penalty
    sigmas_loss = advantage_penalty * ADVANTAGE_FACTOR - SIGMA_ENTROPY_FACTOR * sigma_entropy #+ SIGMA_PENALTY_FACTOR * sigma_penalty
    loss = means_loss + sigmas_loss
    loss = ops.clip(loss, -GRADIENT_CLIPPING_LIMIT, GRADIENT_CLIPPING_LIMIT)
    return loss

def actor_loss_simplified(y_true, y_pred):
   # y_true contains [actions, advantages]
    actions = y_true[:, :2]  # Extract actions
    advantages = y_true[:, 2:4]  # Extract advantages
    sigmas = y_true[:, -2:]
    advantages = ops.mean(advantages, axis=-1)

    sigmas = tf.clip_by_value(sigmas, SIGMA_MIN, SIGMA_MAX)

    # y_pred is a list of outputs: [means, sigmas]
    means = y_pred[0]
    #sigmas = (sigmas * SIGMA_SCALING) + SIGMA_LIMIT_MIN

    # Compute Gaussian log-probabilities
    log_probs = -0.5 * ops.sum(((actions - means) / (sigmas + 1e-8))**2 + 2 * ops.log(sigmas + 1e-8) + ops.log(2 * np.pi), axis=1)
    #tf.print('log probs:', log_probs, summarize=-1)

    if False:
        tf.print('log probs', log_probs)
        tf.print('action penalty', action_penalty)
        tf.print('sigma penalty', sigma_penalty)

    action_penalty = ops.sum(ops.abs(means))

    # Scale log-probabilities by advantages
    loss = ops.mean(-log_probs * advantages) + ACTION_PENALTY_FACTOR * action_penalty

    loss = ops.clip(loss, -GRADIENT_CLIPPING_LIMIT, GRADIENT_CLIPPING_LIMIT)
    return loss

def weighted_MSE(y_true, y_pred):
    _weight = 1
    return ops.mean(_weight * ops.square(y_true - y_pred))

############################################################
# TRAINER ##################################################
############################################################

class Trainer:
    actor = None
    critic = None

    sigma_schedule = Scheduler(SIGMA_INIT_VALUE, SIGMA_DECAY)
    
    # training histories
    trajectory_history = []
    states_history = []
    action_history = []
    reward_history = []
    adjusted_trajectory_history = []

    # utils and logs
    image_processor = ImageProcessor()
    log = Log(verbose=VERBOSE)
    critic_mean_log = []
    critic_var_log = []
    critic_min_log = []
    critic_max_log = []
    action_mean_log = []
    action_max_log = []
    action_min_log = []
    actor_output = []
    loss_actor_log = LossHistory(type='actor')
    loss_critic_log = LossHistory(type='critic')
    sigma_mean_log = []
    sigma_min_log = []
    sigma_max_log = []
    means_mean_log = []
    means_min_log = []
    means_max_log = []
    sigma_log = []

    def __init__(self, model=None, **kwargs):
        if model == None:
            self.new_model(**kwargs)
        elif model == 'ignore':
            self.actor = None
        else:
            self.load_model(model)

    #####################################################################
    # INFERENCE METHODS
    #####################################################################


    def adjust_trajectory(self, trajectory, template_rewards, exploration_factor=0):
        np.set_printoptions(threshold=np.inf)
        self.trajectory_history.append(trajectory)
        _phases = self._points_to_phases(trajectory)                        # turn two dimensional points of trajectory into phases (one dimensional)
        #print(f'Phases: {_phases}')
        _states = np.array(self._get_states(_phases))                       # batch phases together into sequences of phases with dimension input_size
        #print(f'States: {_states}')
        self.states_history.append(_states)
        _offsets = self._get_offsets(_states, exploration_factor, template_rewards)           # actor inference, returns two dimensional offset
        self.action_history.append(_offsets)
        self.action_mean_log.append(np.mean(_offsets))
        self.action_max_log.append(np.max(_offsets))
        self.action_min_log.append(np.min(_offsets))
        _adjusted_trajectory = self._apply_offsets(_offsets, trajectory)    # add offset to originial trajectory
        self.adjusted_trajectory_history.append(_adjusted_trajectory)
        return _adjusted_trajectory
        
    def _apply_offsets(self, offsets, trajectory):
        # calculate difference between amount of trajectory points and amount of offsets
        _index_offset = len(trajectory) - len(offsets[0])

        _offset_x, _offset_y = offsets

        # add unaccounted for values to trajectory
        _new_trajectory = [trajectory[0]]
        if USE_PHASE_DIFFERENCE:
            _new_trajectory.append(trajectory[1])

        # apply offset
        for _point_index in range(_index_offset, len(trajectory)):
            _new_point_x = trajectory[_point_index][0] + OUTPUT_SCALING * _offset_x[_point_index-_index_offset]
            _new_point_y = trajectory[_point_index][1] + OUTPUT_SCALING * _offset_y[_point_index-_index_offset]
            _new_trajectory.append([_new_point_x, _new_point_y])

        return _new_trajectory
    
    def _log_sigmas_and_means(self, mu1, mu2, sigma1, sigma2):
        _means = [mu1, mu2]
        _sigmas = [sigma1, sigma2]
        self.sigma_mean_log.append(np.mean(_sigmas))
        self.sigma_min_log.append(np.min(_sigmas))
        self.sigma_max_log.append(np.max(_sigmas))
        self.means_mean_log.append(np.mean(_means))
        self.means_min_log.append(np.min(_means))
        self.means_max_log.append(np.max(_means))

    def _get_offsets(self, _states, random_action_probability, template_rewards):
        _actor_output = np.array(self.actor.predict(_states, batch_size=len(_states), verbose=0))
        self.actor_output = _actor_output
        _mus = _actor_output

        _template_rewards = np.tanh(np.array(template_rewards)[:len(_mus)]*CRITIC_PRED_SCALING_FACTOR)
        _sigma = (1-_template_rewards) * self.sigma_schedule()

        # sigma smoothing
        kernel = np.ones(SIGMA_SMOOTHING) / SIGMA_SMOOTHING
        _sigma = np.convolve(_sigma, kernel, mode='same')
        _sigma1 = _sigma
        _sigma2 = _sigma

        if TRAINABLE_SIGMA:
            _mus = _actor_output[0]
            _sigmas = _actor_output[1]
            _sigma1 = np.clip(_sigmas.T[0], SIGMA_MIN, SIGMA_MAX)
            _sigma2 = np.clip(_sigmas.T[1], SIGMA_MIN, SIGMA_MAX)

            if not DIRECT_MEANS_TO_ACTION:
                if USE_CRITIC_INSTEAD_OF_SIGMA:
                    _critic_states = np.hstack((_states, _mus))
                    _critic_output = np.array(self.critic.predict(_critic_states))
                    _adjusted_critic_pred = (1 - ((np.tanh(CRITIC_PRED_SCALING_FACTOR * (self._normalize_advantage_subtract_mean(_critic_output) + CRITIC_PRED_BIAS)) + 1) / 2)) * SIGMA_MAX
                    self.sigma_log = _adjusted_critic_pred
                    _sigma1 = np.squeeze(_adjusted_critic_pred)
                    _sigma2 = np.squeeze(_adjusted_critic_pred)

            else:
                _sigma1 = 0.0001
                _sigma2 = 0.0001

        self._log_sigmas_and_means(_mus.T[0], _mus.T[1], _sigma1, _sigma2)

        _offset_x = np.random.normal(loc=_mus.T[0], scale=np.clip(_sigma1, 0, 100))
        _offset_y = np.random.normal(loc=_mus.T[1], scale=np.clip(_sigma1, 0, 100))

        if random_action_probability:
            #_offsets = np.zeros(np.shape(_offsets))
            
            # exchange some offsets with a random offset with probability exploration factor
            for _offset_index in range(len(_offset_x)):
                if np.random.random() < random_action_probability:
                    _offset_x[_offset_index] = np.random.normal(loc=0, scale=RANDOM_ACTION_SCALE)

        return [_offset_x, _offset_y]
    
    def _get_state(self, phases, index):
            _state = []
            for _offset_from_current_point in range(-(INPUT_DIM - NUM_LEADING_POINTS), NUM_LEADING_POINTS):
                try:
                    _state.append(phases[index+_offset_from_current_point])
                except:
                    _state.append(0)
            return _state

    def _get_states_old(self, phases):
        _phases = phases

        if USE_PHASE_DIFFERENCE:
            _phases = self._get_phase_difference(phases)
        
        self.log(f'Phase (-difference): {_phases}')
        
        _states = []

        for _current_point_index in range(len(_phases)):
            _states.append(self._get_state(_phases, _current_point_index))
        return _states
    
    def _get_states(self, phases):
        _phases = phases
        _normalize_factor = 2 * pi
        if USE_PHASE_DIFFERENCE:
            _phases = np.array(self._get_phase_difference(phases))
            _normalize_factor = pi
        if NORMALIZE_STATES:
            _phases = (np.array(_phases) / _normalize_factor) + 0.5

        _phases = np.append(np.zeros(INPUT_DIM - NUM_LEADING_POINTS), _phases)
        window_size = INPUT_DIM
        _states = np.array([_phases[i:i + window_size] for i in range(len(_phases) - window_size + 1)])
        
        _states = self._shift_to_range(_states, 0.5)
        return _states
    
    def _get_phase_difference(self, phases):
        _phase_differences = []

        for _index in range(1, len(phases)):
            
            _phase = phases[_index]
            _prev_phase = phases[_index - 1]
            _phase_difference = _phase-_prev_phase

            if abs(_phase_difference) > pi:
                _phase_difference = -np.sign(_phase_difference) * ((2 * pi) - abs(_phase_difference))
            
            _phase_differences.append(_phase_difference)
        
        return _phase_differences
                
    def _get_phase(self, point, prev_point):
        _pointing_vector = [point[0]-prev_point[0], point[1]-prev_point[1]]
        _phase = atan2(_pointing_vector[1], _pointing_vector[0])
        return _phase
    
    def _points_to_phases(self, trajectory):
        _points = np.array(trajectory)[1:]
        _prev_points = np.array(trajectory)[:-1]
        _direction_vectors = _points - _prev_points
        _direction_vectors = _direction_vectors.T
        _phases = np.arctan2(_direction_vectors[1], _direction_vectors[0])
        return _phases
    
    def _points_to_phases_old(self, trajectory):
        _phases = []
        for _i in range(1, len(trajectory)):
            _point = trajectory[_i]
            _prev_point = trajectory[_i -1]
            _phases.append(self._get_phase(_point, _prev_point))
        return _phases
    
    def _get_adjusted_states(self, states, adjusted_phases):
        _adjusted_phases = np.zeros([INPUT_DIM-NUM_LEADING_POINTS])
        _adjusted_phases = np.append(_adjusted_phases, adjusted_phases)
        _new_states = np.copy(states)

        _normalize_factor = 2 * pi
        if USE_PHASE_DIFFERENCE:
            _adjusted_phases = self._get_phase_difference(_adjusted_phases)
            _normalize_factor = pi
        if NORMALIZE_STATES:
            _adjusted_phases = (np.array(_adjusted_phases) / _normalize_factor) + 0.5
        
        if len(states) != len(_adjusted_phases):
            self.log(f'def _get_adjusted_states(): "Length of states ({len(states)}) and adjusted phases ({len(_adjusted_phases)}) does not match"')

        for _index in range(len(states)):
            _new_states[_index][:INPUT_DIM-NUM_LEADING_POINTS] = _adjusted_phases[_index:_index+INPUT_DIM-NUM_LEADING_POINTS]
        
        _new_states = self._shift_to_range(_new_states, 0.5)
        return _new_states
    
    def _normalize_to_range_incl_neg(self, data):
        return 2 * (data - np.min(data)) / (np.max(data) - np.min(data)) - 1
    
    def _normalize_to_range_pos(self, data):
        return (data - np.min(data)) / (np.max(data) - np.min(data))
    
    def _shift_to_range(self, data, inv_factor):
        """Input data must be in range [0, 1]"""
        return (data * (1 - inv_factor)) + inv_factor


    #####################################################################
    # TRAINING METHODS
    #####################################################################

    def train(self, reward, template_reward, train_actor=True):
        if TRANSFORMER_CRITIC:
            return self._update_actor_and_critic_transformer_based(reward, template_reward, train_actor)
        else:
            return self._update_actor_and_critic_standard(reward, template_reward, train_actor)
    
    def _update_actor_and_critic_transformer_based(self, reward, train_actor):
        _original_trajectory = self.trajectory_history[-1]
        _original_phases = self._points_to_phases(_original_trajectory)
        _states = self.states_history[-1]
        _actions = self.action_history[-1]

        # generate states where the offset is applied to (and only to) the center point, therefore representing clear state transistions
        _adjusted_trajectory = self.adjusted_trajectory_history[-1]
        _adjusted_phases = self._points_to_phases(_adjusted_trajectory)

        # CRITIC ########################################################################################################################

        _critic_action_based_input = np.zeros(TRANSFORMER_CRITIC_DIM)
        _critic_action_based_input[:len(_original_phases)] = _original_phases
        _critic_action_based_input[int(TRANSFORMER_CRITIC_DIM/2):int(TRANSFORMER_CRITIC_DIM/2)+len(_adjusted_phases)] = _adjusted_phases
        _critic_action_based_input = _critic_action_based_input.reshape(-1, 1).T

        _critic_policy_based_input = np.zeros(TRANSFORMER_CRITIC_DIM)
        _critic_policy_based_input[:len(_original_phases)] = _original_phases
        _critic_policy_based_input[int(TRANSFORMER_CRITIC_DIM/2):int(TRANSFORMER_CRITIC_DIM/2)+len(_original_phases)] = _original_phases
        _critic_policy_based_input = _critic_policy_based_input.reshape(-1, 1).T

        #_reward = np.zeros(TRANSFORMER_CRITIC_DIM)
        #_reward[:len(_original_phases)] = np.repeat(reward, len(_original_phases))
        #_reward[int(TRANSFORMER_CRITIC_DIM/2):int(TRANSFORMER_CRITIC_DIM/2)+len(_original_phases)] = np.repeat(reward, len(_original_phases))
        _reward = np.repeat(reward, int(TRANSFORMER_CRITIC_DIM/2))
        _reward = _reward.reshape(-1, 1).T

        self.critic.fit(_critic_action_based_input, _reward, batch_size=64, callbacks=[self.loss_critic_log])

        _critic_predictions_action_taken = self.critic.predict(_critic_action_based_input, verbose=0)
        _critic_predictions_policy = self.critic.predict(_critic_policy_based_input, verbose=0)

        _critic_mean = np.mean(_critic_predictions_action_taken)
        self.critic_mean_log.append(_critic_mean)
        _critic_var = np.var(_critic_predictions_action_taken)
        self.critic_var_log.append(_critic_var)
        _critic_min = np.min(_critic_predictions_action_taken)
        self.critic_min_log.append(_critic_min)
        _critic_max = np.max(_critic_predictions_action_taken)
        self.critic_max_log.append(_critic_max)
        print(f'Critic | mean: {_critic_mean}\tvar: {_critic_var}\tmin: {_critic_min}\tmax: {_critic_max}')
        
        # ACTOR #####################################################################################################################

        _actions = np.array(_actions)
        _actions = _actions.squeeze().T

        # calc advantage
        _advantage = _critic_predictions_action_taken - _critic_predictions_policy
        _advantage = _advantage.reshape(-1, 1)
        _advantage = _advantage[:len(_actions)]
        _advantage = self._normalize_advantage_keep_mean(_advantage)
        #_advantage = self._normalize_advantage(_advantage)
        #_advantage = self._normalize_to_range_incl_neg(_advantage)
        #_advantage = np.repeat(_advantage, 2, axis=1)

        # actor ytrue vector
        _actor_ytrue = tf.concat([_actions, _advantage], axis=1)

        """ _noise = np.random.normal(loc=0.0, scale=0.1, size=_states.shape)
        _states = _states + _noise
        _states = _states * 0.1 # scale input states to make network more sensitive to small changes """

        if train_actor:
            self.actor.fit(_states, _actor_ytrue, callbacks=[self.loss_actor_log])

        self.action_history.clear()
        self.states_history.clear()
        self.adjusted_trajectory_history.clear()
        self.trajectory_history.clear()

        #return np.abs(self._normalize_to_range_incl_neg(_critic_predictions_with_actions).T)[0]
        return np.array(_advantage).T[0], np.array(self.actor_output).T

    def _update_actor_and_critic_standard(self, reward, template_reward, train_actor):
        _original_trajectory = self.trajectory_history[-1]
        _original_phases = self._points_to_phases(_original_trajectory)
        _states = self.states_history[-1]
        _actions = np.array(self.action_history[-1])

        if False:
            # generate states where the offset is applied to (and only to) the center point, therefore representing clear state transistions
            _adjusted_trajectory = self.adjusted_trajectory_history[-1]
            _adjusted_phases = self._points_to_phases(_adjusted_trajectory)
            _adjusted_states = self._get_adjusted_states(_states, _adjusted_phases)
            
            if COMBINE_STATES_FOR_CRITIC:
                _adjusted_states = np.hstack((_adjusted_states, _states))
                _original_states = np.hstack((_states, _states))

                if ADD_PROGRESS_INDICATOR:
                    _num_of_states = np.shape(_adjusted_states)[0]
                    _progress_indicators = np.arange(_num_of_states)
                    _progress_indicators = _progress_indicators / _num_of_states
                    _progress_indicators = _progress_indicators.reshape(-1 ,1)
                    _adjusted_states = np.hstack((_adjusted_states, _progress_indicators))
                    _original_states = np.hstack((_original_states, _progress_indicators))

        _critic_states_template = np.hstack((_states, np.zeros(np.shape(_actions.T))))
        _critic_states_action_based = np.hstack((_states, _actions.T))
        _critic_states_policy_based = np.hstack((_states, self.actor_output))
        if TRAINABLE_SIGMA:
            _critic_states_policy_based = np.hstack((_states, self.actor_output[0]))

        _critic_predictions_template = self.critic.predict(np.array(_critic_states_template), verbose=0)
        _critic_predictions_policy = self.critic.predict(np.array(_critic_states_policy_based), verbose=0)
        _critic_predictions_action_taken = self.critic.predict(np.array(_critic_states_action_based), verbose=0)

        # CRITIC ########

        _reward = []
        if SPARSE_REWARDS and GRANULAR_REWARD:
            _prev_reward = reward[0]
            for _r in reward:
                if _r != _prev_reward:
                    _reward.append(_prev_reward)
                else:
                    _reward.append(0)
                _prev_reward = _r
        else:
            _reward = reward

        _reward = np.array(_reward).reshape(-1, 1)

        _v_targets = REWARD_DISCOUNT * _critic_predictions_policy
        _v_targets = _v_targets[1:]

        if STEP_WISE_REWARD: # This means the final reward is given at every point
            _repeated_reward = np.repeat(_reward, len(_v_targets), axis=0)
            _v_targets = _repeated_reward[:len(_v_targets)] + _v_targets

        if GRANULAR_REWARD and False: # Alternative reward calculation method that allows to calculate a reward in intervals
            _v_targets = _reward[:len(_v_targets)] + _v_targets

        _v_targets = np.append(_v_targets, np.mean(_reward))
        _v_targets = _v_targets.reshape(-1, 1)

        # With these value targets the network learns to predict the reward of each state directly instead of predicting the cumulative reward
        if not CUMULATIVE_VALUE_TRAINING:
            _v_targets = np.repeat(_reward, len(_v_targets), axis=0)

        _critic_mean = np.mean(_critic_predictions_action_taken)
        self.critic_mean_log.append(_critic_mean)
        _critic_var = np.var(_critic_predictions_action_taken)
        self.critic_var_log.append(_critic_var)
        _critic_min = np.min(_critic_predictions_action_taken)
        self.critic_min_log.append(_critic_min)
        _critic_max = np.max(_critic_predictions_action_taken)
        self.critic_max_log.append(np.max(_critic_predictions_action_taken))

        print(f'Critic | mean: {_critic_mean}\tvar: {_critic_var}\tmin: {_critic_min}\tmax: {_critic_max}')

        self.critic.fit(_critic_states_action_based, _v_targets, batch_size=len(_critic_states_action_based), callbacks=[self.loss_critic_log])
            
        # ACTOR #########

        _actions = np.array(_actions)
        _actions = _actions.squeeze().T

        # calc advantage
        #_advantage = _v_targets - _critic_predictions_without_actions
        _advantage = _critic_predictions_action_taken - _critic_predictions_policy
        if not CUMULATIVE_VALUE_TRAINING:
            _advantage = _critic_predictions_action_taken - _critic_predictions_policy
        #_advantage = reward.reshape(-1, 1)
        _advantage = _advantage[:len(_actions)]
        _advantage = self._normalize_advantage_subtract_mean(_advantage)
        #_advantage = self._normalize_advantage(_advantage)

        _actor_ytrue = tf.concat([_actions, _advantage], axis=1)

        if COMPARISON_TRAINING is not None:
            _template_reward = np.array(template_reward).reshape(-1, 1)
            _advantage = _reward - _template_reward
            _advantage = _advantage[:len(_actions)]
            _advantage = self._normalize_advantage_keep_mean(_advantage)
            _advantage_mean = np.mean(_advantage)
            #_advantage = np.clip(_advantage, -1, 2)

        if REWARD_LABELING:
            _non_linear_advantage = np.tanh((_advantage + 0.25) * 1) 
            _weighted_action_inverse = _actions - np.abs(_advantage) * np.sign(_actions)
            _means_true = np.where(_advantage > 0, _actions, self.actor_output * (0.5*_advantage + 1))
            _adjusted_critic_pred = self._normalize_advantage_subtract_mean(_critic_predictions_action_taken) #(np.tanh(CRITIC_PRED_SCALING_FACTOR * (self._normalize_advantage_subtract_mean(_critic_predictions_action_taken) + CRITIC_PRED_BIAS)) + 1) / 2
            _sigmas_true = self.actor_output[1] * 0 + (1 - _adjusted_critic_pred) * SIGMA_TRUE_SCALING
            _sigmas_true = np.where(self.actor_output[0] > SIGMA_MAX, SIGMA_MAX, _sigmas_true)
            _advantage = _advantage # self._normalize_advantage_subtract_mean(_critic_predictions_template)
            
            # new approach
            #_means_true = np.append(np.zeros(np.shape(_means_true[:3])), _means_true, axis=0)
            #_means_true = _means_true[:-3]
            _advantage_mask = np.where((_advantage > 0.3) | (_advantage < -3), 1, 0)
            _advantage_mask = np.squeeze(_advantage_mask)
            _reward_mask = np.where(_critic_predictions_template < 0.1, 1, 0)
            _mask = np.where((_advantage_mask == 1) & (_reward_mask == 1), 1, 0)

            _kernel = np.ones(SIGMA_SMOOTHING) / SIGMA_SMOOTHING
            _advantage_mask = np.convolve(_advantage_mask, _kernel, mode='same')
            
            _masked_states = _states[_advantage_mask > 0]
            _means_true = _means_true[_advantage_mask > 0]
            
            _advantage = np.squeeze(_advantage)
            _advantage = np.where(_advantage_mask, _advantage, [0])
            _advantage = _advantage.reshape(-1, 1)

            #_actor_ytrue = tf.concat([_actions, _advantage, self.sigma_log], axis=1)
        
        print(_means_true)

        if train_actor:
            if TRAINABLE_SIGMA:
                self.actor.fit(_states, [_means_true, _sigmas_true], batch_size=64, callbacks=[self.loss_actor_log])
            else:
                self.actor.fit(_masked_states, _means_true, batch_size=64, callbacks=[self.loss_actor_log])

        self.action_history.clear()
        self.states_history.clear()
        self.adjusted_trajectory_history.clear()
        self.trajectory_history.clear()

        #return np.abs(self._normalize_to_range_incl_neg(_critic_predictions_with_actions).T)[0]
        return np.array(_advantage).T[0], np.array(_adjusted_critic_pred)

    def _reshape_vector(self, state):
        _state = np.array(state)
        _state = _state.reshape(1, -1)
        return _state
    
    def _normalize_advantage_subtract_mean(self, advantage):
        return (advantage - tf.reduce_mean(advantage)) / (tf.math.reduce_std(advantage) + 1e-8)
    
    def _normalize_advantage_keep_mean(self, advantage):
        return advantage / (tf.math.reduce_std(advantage) + 1e-8)

    #####################################################################
    # MODEL CREATION, SAVING and LOADING
    #####################################################################

    def new_model(self, input_size=INPUT_DIM, output_size=ACTION_DIM, hidden_layer_size=HIDDEN_LAYER_DIM_ACTOR):
        # create critic ################################################
        if TRANSFORMER_CRITIC:
            _inputs_critic = keras.layers.Input(shape=(TRANSFORMER_CRITIC_DIM,))
            _hidden_1_critic = keras.layers.Dense(TRANSFORMER_CRITIC_DIM, activation="relu")(_inputs_critic)
            _hidden_2_critic = keras.layers.Dense(TRANSFORMER_CRITIC_DIM, activation="relu")(_hidden_1_critic)
            _output_critic = keras.layers.Dense(int(TRANSFORMER_CRITIC_DIM/2), activation='sigmoid')(_hidden_2_critic)
            self.critic = Model(inputs=_inputs_critic, outputs=_output_critic)

        else:
            if False:
                _critic_input_size = input_size
                if COMBINE_STATES_FOR_CRITIC:
                    _critic_input_size = _critic_input_size * 2
                if ADD_PROGRESS_INDICATOR:
                    _critic_input_size += 1

            _critic_input_size = input_size + ACTION_DIM

            _inputs_critic = keras.layers.Input(shape=(_critic_input_size,))
            _hidden_1_critic = keras.layers.Dense(HIDDEN_LAYER_DIM_CRITIC, activation="relu")(_inputs_critic)
            _hidden_2_critic = keras.layers.Dense(HIDDEN_LAYER_DIM_CRITIC, activation="relu")(_hidden_1_critic)
            _output_critic = keras.layers.Dense(1, activation='softplus')(_hidden_2_critic)
            self.critic = Model(inputs=_inputs_critic, outputs=_output_critic)

        # create actor ##################################################

        _inputs_actor = keras.layers.Input(shape=(input_size,)) # if adding conv layer, do (input_size, 1)
        #_conv_layer_actor = Conv1D(filters=32, kernel_size=5, activation='relu', padding='same')(_inputs_actor)
        #_pool_layer_actor = MaxPool1D(pool_size=2)(_conv_layer_actor)
        #_flattened_layer_actor = Flatten()(_pool_layer_actor)

        _hidden_1_actor = Dense(HIDDEN_LAYER_DIM_ACTOR, activation="relu")(_inputs_actor)
        _hidden_2_actor = Dense(HIDDEN_LAYER_DIM_ACTOR, activation="relu")(_hidden_1_actor)
        _hidden_3_actor = Dense(HIDDEN_LAYER_DIM_ACTOR, activation="relu")(_hidden_2_actor)
        _hidden_4_actor = Dense(HIDDEN_LAYER_DIM_ACTOR, activation="relu")(_hidden_3_actor)
        _hidden_5_actor = Dense(HIDDEN_LAYER_DIM_ACTOR, activation="relu")(_hidden_4_actor)
        _hidden_6_actor = Dense(HIDDEN_LAYER_DIM_ACTOR, activation="relu")(_hidden_5_actor)

        _output_mu1 = Dense(1, activation='tanh', name='mu1')(_hidden_6_actor)
        _output_mu2 = Dense(1, activation='tanh', name='mu2')(_hidden_6_actor)
        merged_mus = Lambda(lambda x: tf.concat(x, axis=-1), name="merged_mus")([_output_mu1, _output_mu2])

        if TRAINABLE_SIGMA:
            _sigma_initializer = keras.initializers.RandomUniform(-SIGMA_INIT_WEIGHT_LIMIT, SIGMA_INIT_WEIGHT_LIMIT)
            _hidden_1_sigma = Dense(HIDDEN_LAYER_DIM_ACTOR, activation='relu')(_hidden_2_actor)
            _hidden_2_sigma = Dense(HIDDEN_LAYER_DIM_ACTOR, activation='relu')(_hidden_1_sigma)
            _output_sigma1 = Dense(1, activation='softplus', name='sigma1', kernel_initializer=_sigma_initializer)(_hidden_2_sigma)
            _output_sigma1 = Lambda(lambda x: SIGMA_OUTPUT_SCALING * x)(_output_sigma1)
            _output_sigma2 = Dense(1, activation='softplus', name='sigma2', kernel_initializer=_sigma_initializer)(_hidden_2_sigma)
            _output_sigma2 = Lambda(lambda x: SIGMA_OUTPUT_SCALING * x)(_output_sigma2)
            merged_sigmas = Lambda(lambda x: tf.concat(x, axis=-1), name="merged_sigmas")([_output_sigma1, _output_sigma2])

            self.actor = Model(inputs=_inputs_actor, outputs=[merged_mus, merged_sigmas])

        else:
            self.actor = Model(inputs=_inputs_actor, outputs=merged_mus)

        # compile #######################################################
        _optimizer_critic = keras.optimizers.Adam(learning_rate=LR_CRITIC)
        _optimizer_actor = keras.optimizers.Adam(learning_rate=LR_ACTOR)
        _loss_critic = keras.losses.MeanAbsoluteError() #weighted_MSE
        _loss_mus = keras.losses.MeanSquaredError()
        _loss_sigmas = keras.losses.MeanAbsoluteError() 

        if not REWARD_LABELING:
            _loss_actor = actor_loss
        if TRAINABLE_SIGMA:
            self.actor.compile(optimizer=_optimizer_actor, loss=[_loss_mus, _loss_sigmas])
        else:
            self.actor.compile(optimizer=_optimizer_actor, loss=_loss_mus)
        self.critic.compile(optimizer=_optimizer_critic, loss=_loss_critic, metrics=['accuracy'])

    def load_model(self, model_id):
        # Looks how many models are in directory
        # If model > number of models, return Error
        # else load model from directory
        _script_dir = os.path.dirname(os.path.abspath(__file__))
        _num_of_models = len(os.listdir(os.path.join(_script_dir, f'models')))
        if model_id > _num_of_models:
            print('Model does not exist')
            exit()
        _path = os.path.join(_script_dir, f'models/model_{str(model_id)}.h5')
        model = load_model(_path)

    def save_model(self, model_id=None):
        _script_dir = os.path.dirname(os.path.abspath(__file__))
        _num_of_models = len(os.listdir(os.path.join(_script_dir, f'models')))
        _model_id = 0

        if model_id:
            if model_id > _num_of_models:
                print(f'Chosen model ID is not valid. Will set model id to {_num_of_models} .')
                _model_id = _num_of_models
            else:
                _model_id = model_id
        else:
            _model_id = _num_of_models

        _path = os.path.join(_script_dir, f'models/model_{str(_model_id)}.h5')
        self.actor.save(_path)
