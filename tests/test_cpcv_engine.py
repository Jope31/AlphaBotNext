import pytest
from datetime import datetime, timedelta
import synthetic_history
import math_engine
import numpy as np

def test_cpcv_block_generation():
    dates = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(125)]
    dates.sort()
    
    blocks = synthetic_history.generate_cpcv_blocks(dates, num_blocks=5, purge_buffer_days=1)
    
    assert len(blocks) == 5
    # Since n=125, each block before purging has 25 days.
    # Purging drops 1 day from start/end.
    # First block: start intact, end purged -> 24 days
    assert len(blocks[0]) == 24
    # Middle block: start purged, end purged -> 23 days
    assert len(blocks[1]) == 23
    # Last block: start purged, end intact -> 24 days
    assert len(blocks[4]) == 24

def test_cpcv_path_generation():
    blocks = [[str(i)] for i in range(5)]
    paths = synthetic_history.generate_cpcv_paths(blocks, n_train=3)
    
    # 5 choose 3 = 10 combinations
    assert len(paths) == 10
    
    for train_dates, test_dates in paths:
        assert len(train_dates) == 3
        assert len(test_dates) == 2
        # No overlap
        assert set(train_dates).isdisjoint(set(test_dates))

def test_pbo_calculation():
    # Construct a matrix where IS rank is perfectly negatively correlated with OOS rank
    n_trials = 3
    n_paths = 1
    
    is_matrix = np.array([
        [0.10], # Trial 0: IS bad
        [0.20], # Trial 1: IS medium
        [0.30]  # Trial 2: IS best
    ])
    
    oos_matrix = np.array([
        [0.30], # Trial 0: OOS best
        [0.20], # Trial 1: OOS medium
        [0.10]  # Trial 2: OOS bad
    ])
    
    # IS-best is Trial 2. Its OOS is 0.10. Median OOS is 0.20. 
    # Since 0.10 < 0.20, it is degraded. 1 out of 1 paths degraded -> PBO = 100%
    pbo = math_engine.calculate_pbo(is_matrix, oos_matrix)
    assert pbo == 100.0
