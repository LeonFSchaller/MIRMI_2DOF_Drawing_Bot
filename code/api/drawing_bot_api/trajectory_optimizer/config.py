# model parameters
INPUT_DIM = 30
NUM_LEADING_POINTS = 5
ACTION_DIM = 2
HIDDEN_LAYER_DIM = 256
DISCOUNT = 1
LR_CRITIC = 0.001
LR_ACTOR = 0.001
OUTPUT_SCALING = 3

# Exploration settings
SIGMA = 0.02
SIGMA_DECAY = 0.99
RANDOM_ACTION_PROBABILITY = 0.0#3
RANDOM_ACTION_DECAY = 0.995
RANDOM_ACTION_SCALE = 0.05

SIGMA_SCALING = 0.1
SIGMA_LIMIT_MIN = 0.01

# Options
USE_PHASE_DIFFERENCE = False
NORMALIZE_STATES = True
VERBOSE = 0
SAVE_IMAGE_FREQ = 5
SAVE_SIMPLIFIED = False
COMBINE_STATES_FOR_CRITIC = True
ADD_PROGRESS_INDICATOR = True
REWARD_DISTANCE_CLIPPING = 10
REWARD_NORMALIZATION_MODE = 'sigmoid' # options: 'linear', 'sigmoid'
SPARSE_REWARDS = False
TRAINABLE_SIGMA = True
