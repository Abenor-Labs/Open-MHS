"""A robosuite tabletop with several coloured blocks instead of one.

`Lift` ships a single red cube. Manipulation demos need more than that — something to sort,
stack, or move relative to something else — so this subclass replaces the single object
with a set of colour-coded blocks placed apart from each other on the table.

Colour is the point, not decoration: each block is a distinct, saturated hue so the vision
system can segment them by colour alone. Nothing here reads object poses out of the
simulator for the agent's benefit; see `cell.py` for how perception is actually done.

`self.cube` is kept pointing at the first block so `Lift`'s own reward and success checks
continue to work unchanged.
"""

from __future__ import annotations

import numpy as np
from robosuite.environments.manipulation.lift import Lift
from robosuite.models.arenas import TableArena
from robosuite.models.objects import BoxObject
from robosuite.models.tasks import ManipulationTask
from robosuite.utils.placement_samplers import UniformRandomSampler

#: name -> RGBA. Hues are spread far apart so HSV segmentation cannot confuse two blocks.
BLOCKS: dict[str, list[float]] = {
    "red_block": [0.85, 0.05, 0.05, 1.0],
    "green_block": [0.05, 0.75, 0.15, 1.0],
    "blue_block": [0.05, 0.25, 0.90, 1.0],
    "yellow_block": [0.95, 0.80, 0.05, 1.0],
}

BLOCK_HALF_SIZE = 0.021          # 42 mm cube; the Panda gripper closes comfortably on it


class MultiBlockCell(Lift):
    """`Lift`, but with several coloured blocks spread across the table."""

    def _load_model(self):
        # Skip Lift._load_model entirely — it hardcodes one object — but keep everything
        # ManipulationEnv does above it.
        super(Lift, self)._load_model()

        xpos = self.robots[0].robot_model.base_xpos_offset["table"](self.table_full_size[0])
        self.robots[0].robot_model.set_base_xpos(xpos)

        arena = TableArena(
            table_full_size=self.table_full_size,
            table_friction=self.table_friction,
            table_offset=self.table_offset,
        )
        arena.set_origin([0, 0, 0])

        self.blocks = []
        for name, rgba in BLOCKS.items():
            self.blocks.append(
                BoxObject(
                    name=name,
                    size_min=[BLOCK_HALF_SIZE] * 3,
                    size_max=[BLOCK_HALF_SIZE] * 3,
                    rgba=rgba,
                    rng=self.rng,
                )
            )
        # Lift's reward and success checks reference `self.cube`. Point it at the first
        # block so inherited behaviour keeps working instead of breaking silently.
        self.cube = self.blocks[0]

        # Spread them over the reachable part of the table. `ensure_valid_placement` stops
        # the sampler dropping one block on top of another.
        self.placement_initializer = UniformRandomSampler(
            name="ObjectSampler",
            mujoco_objects=self.blocks,
            x_range=[-0.16, 0.16],
            y_range=[-0.16, 0.16],
            rotation=None,
            ensure_object_boundary_in_range=False,
            ensure_valid_placement=True,
            reference_pos=self.table_offset,
            z_offset=0.01,
            rng=self.rng,
        )

        self.model = ManipulationTask(
            mujoco_arena=arena,
            mujoco_robots=[robot.robot_model for robot in self.robots],
            mujoco_objects=self.blocks,
        )

    def _setup_references(self):
        super()._setup_references()
        self.block_body_ids = {
            block.name: self.sim.model.body_name2id(block.root_body)
            for block in self.blocks
        }

    def block_position(self, name: str) -> np.ndarray:
        """Ground-truth pose of one block. Used only as the vision fallback."""
        return np.asarray(self.sim.data.body_xpos[self.block_body_ids[name]], dtype=float)
