# coding=utf-8
# Copyright 2018 The TF-Agents Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test for tf_agents.utils.py_tf_policy."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os

from absl import flags
from absl.testing import parameterized
import numpy as np
import tensorflow as tf

from tf_agents.environments import time_step as ts
from tf_agents.networks import network
from tf_agents.policies import py_tf_policy
from tf_agents.policies import q_policy
from tf_agents.specs import tensor_spec


class DummyNet(network.Network):

  def __init__(self,
               name=None,
               num_actions=2,
               stateful=True,
               use_constant_initializer=True):
    if stateful:
      state_spec = tensor_spec.TensorSpec(shape=(1,), dtype=tf.float32)
    else:
      state_spec = ()
    super(DummyNet, self).__init__(input_tensor_spec=None,
                                   state_spec=state_spec,
                                   name=name)

    kernel_initializer = None
    bias_initializer = None
    if use_constant_initializer:
      kernel_initializer = tf.compat.v1.initializers.constant(
          [[1, 200], [3, 4]], verify_shape=True)
      bias_initializer = tf.compat.v1.initializers.constant([1, 1],
                                                            verify_shape=True)

    # Store custom layers that can be serialized through the Checkpointable API.
    self._dummy_layers = []
    self._dummy_layers.append(
        tf.keras.layers.Dense(
            num_actions,
            kernel_initializer=kernel_initializer,
            bias_initializer=bias_initializer))

  def call(self, inputs, unused_step_type=None, network_state=()):
    inputs = tf.cast(inputs, tf.float32)
    for layer in self._dummy_layers:
      inputs = layer(inputs)
    return inputs, network_state


# TODO(damienv): This function should belong to nest_utils
def fast_map_structure(func, *structure):
  flat_structure = [tf.nest.flatten(s) for s in structure]
  entries = zip(*flat_structure)

  return tf.nest.pack_sequence_as(structure[0], [func(*x) for x in entries])


class PyTFPolicyTest(tf.test.TestCase, parameterized.TestCase):

  def setUp(self):
    super(PyTFPolicyTest, self).setUp()
    self._obs_spec = tensor_spec.TensorSpec([2], tf.float32, 'obs')
    self._time_step_spec = ts.time_step_spec(self._obs_spec)
    self._action_spec = tensor_spec.BoundedTensorSpec([], tf.int32, 0, 1,
                                                      'action')
    self._tf_policy = q_policy.QPolicy(
        self._time_step_spec,
        self._action_spec,
        q_network=DummyNet())

  def testBuild(self):
    policy = py_tf_policy.PyTFPolicy(self._tf_policy)
    expected_time_step_spec = ts.time_step_spec(
        tensor_spec.to_nest_array_spec(self._obs_spec))
    expected_action_spec = tensor_spec.to_nest_array_spec(self._action_spec)
    self.assertEqual(expected_time_step_spec, policy.time_step_spec)
    self.assertEqual(expected_action_spec, policy.action_spec)

  def testRaiseValueErrorWithoutSession(self):
    policy = py_tf_policy.PyTFPolicy(self._tf_policy)
    with self.assertRaisesRegexp(
        AttributeError,
        "No TensorFlow session-like object was set on this 'PyTFPolicy'.*"):
      policy.get_initial_state()

  @parameterized.parameters([{'batch_size': None}, {'batch_size': 5}])
  def testAssignSession(self, batch_size):
    if tf.executing_eagerly():
      self.skipTest('b/123770140')

    policy = py_tf_policy.PyTFPolicy(self._tf_policy)
    policy.session = tf.compat.v1.Session()
    expected_initial_state = np.zeros([batch_size or 1, 1], dtype=np.float32)
    self.assertTrue(
        np.array_equal(
            policy.get_initial_state(batch_size), expected_initial_state))

  @parameterized.parameters([{'batch_size': None}, {'batch_size': 5}])
  def testZeroState(self, batch_size):
    if tf.executing_eagerly():
      self.skipTest('b/123770140')

    policy = py_tf_policy.PyTFPolicy(self._tf_policy)
    expected_initial_state = np.zeros([batch_size or 1, 1], dtype=np.float32)
    with self.cached_session():
      self.assertTrue(
          np.array_equal(
              policy.get_initial_state(batch_size), expected_initial_state))

  @parameterized.parameters([{'batch_size': None}, {'batch_size': 5}])
  def testAction(self, batch_size):
    if tf.executing_eagerly():
      self.skipTest('b/123770140')

    single_observation = np.array([1, 2], dtype=np.float32)
    time_steps = ts.restart(single_observation)
    if batch_size is not None:
      time_steps = [time_steps] * batch_size
      time_steps = fast_map_structure(lambda *arrays: np.stack(arrays),
                                      *time_steps)
    policy = py_tf_policy.PyTFPolicy(self._tf_policy)

    with self.cached_session():
      policy_state = policy.get_initial_state(batch_size)
      self.evaluate(tf.compat.v1.global_variables_initializer())
      action_steps = policy.action(time_steps, policy_state)
      self.assertEqual(action_steps.action.dtype, np.int32)
      if batch_size is None:
        self.assertEqual(action_steps.action.shape, ())
        self.assertIn(action_steps.action, (0, 1))
        self.assertEqual(action_steps.state, np.zeros([1, 1]))
      else:
        self.assertEqual(action_steps.action.shape, (batch_size,))
        for a in action_steps.action:
          self.assertIn(a, (0, 1))
        self.assertAllEqual(action_steps.state, np.zeros([batch_size, 1]))

  @parameterized.parameters([{'batch_size': None}, {'batch_size': 5}])
  def testSaveRestore(self, batch_size):
    policy_save_path = os.path.join(flags.FLAGS.test_tmpdir, 'policy',
                                    str(batch_size))

    # Construct a policy to be saved under a tf.Graph instance.
    policy_saved_graph = tf.Graph()
    with policy_saved_graph.as_default():
      tf_policy = q_policy.QPolicy(self._time_step_spec, self._action_spec,
                                   DummyNet(use_constant_initializer=False))

      # Parameterized tests reuse temp directories, make no save exists.
      try:
        tf.io.gfile.listdir(policy_save_path)
        tf.io.gfile.rmtree(policy_save_path)
      except tf.errors.NotFoundError:
        pass
      policy_saved = py_tf_policy.PyTFPolicy(tf_policy)
      policy_saved.session = tf.compat.v1.Session(graph=policy_saved_graph)
      policy_saved.initialize(batch_size)
      policy_saved.save(policy_dir=policy_save_path, graph=policy_saved_graph)
      self.assertEqual(
          set(tf.io.gfile.listdir(policy_save_path)),
          set(['checkpoint', 'ckpt-0.data-00000-of-00001', 'ckpt-0.index']))

    # Construct a policy to be restored under another tf.Graph instance.
    policy_restore_graph = tf.Graph()
    with policy_restore_graph.as_default():
      tf_policy = q_policy.QPolicy(self._time_step_spec, self._action_spec,
                                   DummyNet(use_constant_initializer=False))
      policy_restored = py_tf_policy.PyTFPolicy(tf_policy)
      policy_restored.session = tf.compat.v1.Session(graph=policy_restore_graph)
      policy_restored.initialize(batch_size)
      random_init_vals = policy_restored.session.run(tf_policy.variables())
      policy_restored.restore(
          policy_dir=policy_save_path, graph=policy_restore_graph)
      restored_vals = policy_restored.session.run(tf_policy.variables())
      for random_init_var, restored_var in zip(random_init_vals, restored_vals):
        self.assertFalse(np.array_equal(random_init_var, restored_var))

    # Check that variables in the two policies have identical values.
    with policy_restore_graph.as_default():
      restored_values = policy_restored.session.run(
          tf.compat.v1.global_variables())
    with policy_saved_graph.as_default():
      initial_values = policy_saved.session.run(tf.compat.v1.global_variables())

    # Networks have two fully connected layers.
    self.assertLen(initial_values, 4)
    self.assertLen(restored_values, 4)

    for initial_var, restored_var in zip(initial_values, restored_values):
      np.testing.assert_array_equal(initial_var, restored_var)

  def testDeferredBatchingAction(self):
    if tf.executing_eagerly():
      self.skipTest('b/123770140')

    # Construct policy without providing batch_size.
    tf_policy = q_policy.QPolicy(
        self._time_step_spec,
        self._action_spec,
        q_network=DummyNet(stateful=False))
    policy = py_tf_policy.PyTFPolicy(tf_policy)

    # But time_steps have batch_size of 5
    batch_size = 5
    single_observation = np.array([1, 2], dtype=np.float32)
    time_steps = [ts.restart(single_observation)] * batch_size
    time_steps = fast_map_structure(lambda *arrays: np.stack(arrays),
                                    *time_steps)

    with self.cached_session():
      self.evaluate(tf.compat.v1.global_variables_initializer())
      action_steps = policy.action(time_steps)
      self.assertEqual(action_steps.action.shape, (batch_size,))
      for a in action_steps.action:
        self.assertIn(a, (0, 1))
      self.assertAllEqual(action_steps.state, ())

  def testDeferredBatchingStateful(self):
    if tf.executing_eagerly():
      self.skipTest('b/123770140')

    # Construct policy without providing batch_size.
    policy = py_tf_policy.PyTFPolicy(self._tf_policy)

    # But time_steps have batch_size of 5
    batch_size = 5
    single_observation = np.array([1, 2], dtype=np.float32)
    time_steps = [ts.restart(single_observation)] * batch_size
    time_steps = fast_map_structure(lambda *arrays: np.stack(arrays),
                                    *time_steps)

    with self.cached_session():
      initial_state = policy.get_initial_state(batch_size=batch_size)
      self.assertAllEqual(initial_state, np.zeros([5, 1]))
      action_steps = policy.action(time_steps, initial_state)
      self.assertEqual(action_steps.action.shape, (batch_size,))
      for a in action_steps.action:
        self.assertIn(a, (0, 1))
      self.assertAllEqual(action_steps.state, np.zeros([5, 1]))


if __name__ == '__main__':
  tf.test.main()
