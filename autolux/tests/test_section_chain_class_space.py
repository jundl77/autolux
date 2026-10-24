import types

import pytest

from autolux.lib.label_space import NUM_SECTION_CLASSES, SECTION_LABELS
from autolux.lib.section_chain import _check_class_space


def _priors(*classes):
    return types.SimpleNamespace(classes=tuple(classes))


def _full():
    return _priors(*SECTION_LABELS)


def test_the_full_vocabulary_at_every_layer_is_accepted():
    _check_class_space(_full(), NUM_SECTION_CLASSES, SECTION_LABELS)


def test_the_retired_five_class_priors_are_refused_at_chain_construction():
    # The GATED_SPACE flip: the offline decoder may replay a subset space, the
    # chain that lights a room may not.
    with pytest.raises(ValueError, match='full vocabulary'):
        _check_class_space(
            _priors('intro', 'buildup', 'breakdown', 'drop', 'outro'),
            NUM_SECTION_CLASSES,
            SECTION_LABELS,
        )


def test_a_class_outside_the_vocabulary_is_refused():
    with pytest.raises(ValueError, match='chorus'):
        _check_class_space(_priors('intro', 'chorus'), NUM_SECTION_CLASSES, SECTION_LABELS)


def test_a_space_in_the_wrong_order_is_refused_rather_than_decoded_shifted():
    with pytest.raises(ValueError, match='order'):
        _check_class_space(_priors('drop', 'intro', 'outro'), NUM_SECTION_CLASSES, SECTION_LABELS)


def test_a_duplicated_class_is_refused():
    with pytest.raises(ValueError, match='duplicate'):
        _check_class_space(_priors('intro', 'intro'), NUM_SECTION_CLASSES, SECTION_LABELS)


def test_a_model_head_of_the_wrong_width_is_refused():
    with pytest.raises(ValueError, match='label head'):
        _check_class_space(_full(), 5, SECTION_LABELS)


def test_a_graph_that_will_not_declare_its_class_axis_is_refused():
    with pytest.raises(ValueError, match='undeclared'):
        _check_class_space(_full(), None, SECTION_LABELS)


def test_a_config_swept_in_another_space_is_refused():
    with pytest.raises(ValueError, match='swept'):
        _check_class_space(
            _full(), NUM_SECTION_CLASSES, ('intro', 'buildup', 'breakdown', 'drop', 'outro')
        )
