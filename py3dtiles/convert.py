import argparse
import concurrent.futures
import json
import multiprocessing
import os
import pickle
import shutil
import struct
import sys
import time
from collections import namedtuple
from pathlib import Path, PurePath

import numpy as np
import psutil
import zmq
from pyproj import CRS, Transformer

from py3dtiles import TileContentReader
from py3dtiles.constants import MIN_POINT_SIZE
from py3dtiles.points.node import Node
from py3dtiles.points.shared_node_store import SharedNodeStore
from py3dtiles.points.task import las_reader, xyz_reader, node_process, pnts_writer
from py3dtiles.points.transformations import rotation_matrix, angle_between_vectors, vector_product, inverse_matrix, \
    scale_matrix, translation_matrix
from py3dtiles.points.utils import CommandType, ResponseType, compute_spacing, name_to_filename
from py3dtiles.utils import SrsInMissingException

TOTAL_MEMORY_MB = int(psutil.virtual_memory().total / (1024 * 1024))
IPC_URI = "ipc:///tmp/py3dtiles1"

OctreeMetadata = namedtuple('OctreeMetadata', ['aabb', 'spacing', 'scale'])


def write_tileset(out_folder, octree_metadata, offset, scale, rotation_matrix, include_rgb):
    # compute tile transform matrix
    if rotation_matrix is None:
        transform = np.identity(4)
    else:
        transform = inverse_matrix(rotation_matrix)
    transform = np.dot(transform, scale_matrix(1.0 / scale[0]))
    transform = np.dot(translation_matrix(offset), transform)

    # build fake points
    root_node = Node('', octree_metadata.aabb, octree_metadata.spacing * 2)
    root_node.children = []
    inv_aabb_size = (1.0 / np.maximum(MIN_POINT_SIZE, octree_metadata.aabb[1] - octree_metadata.aabb[0])).astype(np.float32)
    for child in range(8):
        ondisk_tile = name_to_filename(out_folder, str(child).encode('ascii'), '.pnts')
        if os.path.exists(ondisk_tile):
            tile_content = TileContentReader.read_file(ondisk_tile)
            fth = tile_content.body.feature_table.header
            xyz = tile_content.body.feature_table.body.positions_arr.view(np.float32).reshape((fth.points_length, 3))
            if include_rgb:
                rgb = tile_content.body.feature_table.body.colors_arr.reshape((fth.points_length, 3))
            else:
                rgb = np.zeros(xyz.shape, dtype=np.uint8)

            root_node.grid.insert(
                octree_metadata.aabb[0].astype(np.float32),
                inv_aabb_size,
                xyz.copy(),
                rgb)

    pnts_writer.node_to_pnts(''.encode('ascii'), root_node, out_folder, include_rgb)

    executor = concurrent.futures.ProcessPoolExecutor()
    root_tileset = Node.to_tileset(executor, ''.encode('ascii'), octree_metadata.aabb, octree_metadata.spacing, out_folder, scale)
    executor.shutdown()

    root_tileset['transform'] = transform.T.reshape(16).tolist()
    root_tileset['refine'] = 'REPLACE'
    for child in root_tileset['children']:
        child['refine'] = 'ADD'

    tileset = {
        'asset': {
            'version': '1.0',
        },
        'geometricError': np.linalg.norm(
            octree_metadata.aabb[1] - octree_metadata.aabb[0]) / scale[0],
        'root': root_tileset,
    }

    tileset_path = Path(out_folder) / "tileset.json"
    with tileset_path.open('w') as f:
        f.write(json.dumps(tileset))


def make_rotation_matrix(z1, z2):
    v0 = z1 / np.linalg.norm(z1)
    v1 = z2 / np.linalg.norm(z2)

    return rotation_matrix(
        angle_between_vectors(v0, v1),
        vector_product(v0, v1))


# Worker
def zmq_process(*args):
    process = Worker(*args)
    process.run()


class Worker:
    """
    This class waits from jobs commands from the Zmq socket.
    """
    def __init__(self, activity_graph, transformer, octree_metadata, folder, write_rgb, verbosity):
        self.activity_graph = activity_graph
        self.transformer = transformer
        self.octree_metadata = octree_metadata
        self.folder = folder
        self.write_rgb = write_rgb
        self.verbosity = verbosity

        # Socket to receive messages on
        self.context = zmq.Context()
        self.skt = self.context.socket(zmq.DEALER)

    def run(self):
        self.skt.connect(IPC_URI)

        startup_time = time.time()
        idle_time = 0

        if self.activity_graph:
            activity = open('activity.{}.csv'.format(os.getpid()), 'w')

        # notify we're ready
        self.skt.send_multipart([ResponseType.IDLE.value])

        while True:
            before = time.time() - startup_time
            self.skt.poll()
            after = time.time() - startup_time

            idle_time += after - before

            message = self.skt.recv_multipart()
            content = message[1:]
            command = content[0]

            delta = time.time() - pickle.loads(message[0])
            if delta > 0.01 and self.verbosity >= 1:
                print('{} / {} : Delta time: {}'.format(os.getpid(), round(after, 2), round(delta, 3)))

            if command == CommandType.READ_FILE.value:
                self.execute_read_file(content)
                command_type = 1
            elif command == CommandType.PROCESS_JOBS.value:
                self.execute_process_jobs(content)
                command_type = 2
            elif command == CommandType.WRITE_PNTS.value:
                self.execute_write_pnts(content)
                command_type = 3
            elif command == CommandType.SHUTDOWN.value:
                break  # ack
            else:
                raise NotImplementedError(f'Unknown command {command}')

            # notify we're idle
            self.skt.send_multipart([ResponseType.IDLE.value])

            if self.activity_graph:
                print(f'{before}, {command_type}', file=activity)
                print(f'{before}, 0', file=activity)
                print(f'{after}, 0', file=activity)
                print(f'{after}, {command_type}', file=activity)

        if self.activity_graph:
            activity.close()

        if self.verbosity >= 1:
            print('total: {} sec, idle: {}'.format(
                round(time.time() - startup_time, 1),
                round(idle_time, 1))
            )

        self.skt.send_multipart([ResponseType.HALTED.value])

    def execute_read_file(self, content):
        parameters = pickle.loads(content[1])

        ext = PurePath(parameters['filename']).suffix
        init_reader_fn = las_reader.run if ext in ('.las', '.laz') else xyz_reader.run
        init_reader_fn(
            parameters['filename'],
            parameters['offset_scale'],
            parameters['portion'],
            self.skt,
            self.transformer,
            self.verbosity
        )

    def execute_write_pnts(self, content):
        pnts_writer.run(self.skt, content[2], content[1], self.folder, self.write_rgb)

    def execute_process_jobs(self, content):
        node_process.run(
            content[1:],
            self.octree_metadata,
            self.skt,
            self.verbosity
        )


# Manager
class ZmqManager:
    """
    This class sends messages to the workers.
    We can also request general status.
    """
    def __init__(self, number_of_jobs: int, process_args: tuple):
        """
        For the process_args argument, see the init method of Worker
        to get the list of needed parameters.
        """
        self.context = zmq.Context()

        self.number_of_jobs = number_of_jobs

        self.socket = self.context.socket(zmq.ROUTER)
        self.socket.bind(IPC_URI)

        self.processes = [
            multiprocessing.Process(target=zmq_process, args=process_args)
            for _ in range(number_of_jobs)
        ]
        [p.start() for p in self.processes]

        self.activities = [p.pid for p in self.processes]
        self.idle_clients = []

        self.killing_processes = False
        self.number_processes_killed = 0
        self.time_waiting_an_idle_process = 0

    def send_to_process(self, message):
        if not self.idle_clients:
            raise ValueError("idle_clients is empty")
        self.socket.send_multipart([self.idle_clients.pop(), pickle.dumps(time.time())] + message)

    def send_to_all_process(self, message):
        if not self.idle_clients:
            raise ValueError("idle_clients is empty")
        for client in self.idle_clients:
            self.socket.send_multipart([client, pickle.dumps(time.time())] + message)
        self.idle_clients.clear()

    def can_queue_more_jobs(self):
        return len(self.idle_clients) != 0

    def add_idle_client(self, client_id):
        if client_id in self.idle_clients:
            raise ValueError(f"The client id {client_id} is already in idle_clients")
        self.idle_clients.append(client_id)

    def are_all_processes_idle(self):
        return len(self.idle_clients) == self.number_of_jobs

    def are_all_processes_killed(self):
        return self.number_processes_killed == self.number_of_jobs

    def kill_all_processes(self):
        self.send_to_all_process([CommandType.SHUTDOWN.value])
        self.killing_processes = True

    def terminate_all_processes(self):
        for p in self.processes:
            p.terminate()


def is_ancestor(node_name, ancestor):
    """
    Example, the tile 22 is ancestor of 22458
    Particular case, the tile 22 is ancestor of 22
    """
    return len(ancestor) <= len(node_name) and node_name[0:len(ancestor)] == ancestor


def is_ancestor_in_list(node_name, ancestors):
    for ancestor in ancestors:
        if not ancestor or is_ancestor(node_name, ancestor):
            return True
    return False


def can_pnts_be_written(node_name, finished_node, input_nodes, active_nodes):
    return (
        is_ancestor(node_name, finished_node)
        and not is_ancestor_in_list(node_name, active_nodes)
        and not is_ancestor_in_list(node_name, input_nodes))


class State:
    def __init__(self, pointcloud_file_portions, max_reading_jobs: int):
        self.processed_points = 0
        self.max_point_in_progress = 60_000_000
        self.points_in_progress = 0
        self.points_in_pnts = 0

        # pointcloud_file_portions is a list of tuple (filename, (start offset, end offset))
        self.point_cloud_file_parts = pointcloud_file_portions
        self.max_reading_jobs = max_reading_jobs
        self.number_of_reading_jobs = 0
        self.number_of_writing_jobs = 0

        # node_to_process is a dictionary of tasks,
        # each entry is a tile identified by its name (a string of numbers)
        # so for each entry, it is a list of tasks
        # a task is a tuple (list of points, point_count)
        # points is a dictionary {xyz: list of coordinates, color: the associated color}
        self.node_to_process = {}
        # when a node is sent to a process, the item moves to processing_nodes
        # the structure is different. The key remains the node name. But the value is : (len(tasks), point_count, now)
        # these values is for loging
        self.processing_nodes = {}
        # when processing is finished, move the tile name in processed_nodes
        # since the content is at this stage, stored in the node_store,
        # just keep the name of the node.
        # This list will be filled until the writing could be started.
        self.waiting_writing_nodes = []
        # when the node is writing, its name is moved from waiting_writing_nodes to pnts_to_writing
        # the data to write are stored in a node object.
        self.pnts_to_writing = []

    def is_reading_finish(self):
        return not self.point_cloud_file_parts and self.number_of_reading_jobs == 0

    def add_tasks_to_process(self, node_name, task, point_count):
        if point_count <= 0:
            raise ValueError("point_count should be strictly positive, currently", point_count)

        if node_name not in self.node_to_process:
            self.node_to_process[node_name] = ([task], point_count)
        else:
            tasks, count = self.node_to_process[node_name]
            tasks.append(task)
            self.node_to_process[node_name] = (tasks, count + point_count)

    def can_add_reading_jobs(self):
        return (
            self.point_cloud_file_parts
            and self.points_in_progress < self.max_point_in_progress
            and self.number_of_reading_jobs < self.max_reading_jobs
        )

    def print_debug(self):
        print('{:^16}|{:^8}|{:^8}|{:^8}'.format('Step', 'Input', 'Active', 'Inactive'))
        print('{:^16}|{:^8}|{:^8}|{:^8}'.format(
            'Reader',
            len(self.point_cloud_file_parts),
            self.number_of_reading_jobs,
            ''))
        print('{:^16}|{:^8}|{:^8}|{:^8}'.format(
            'Node process',
            len(self.node_to_process),
            len(self.processing_nodes),
            len(self.waiting_writing_nodes)))
        print('{:^16}|{:^8}|{:^8}|{:^8}'.format(
            'Pnts writer',
            len(self.pnts_to_writing),
            self.number_of_writing_jobs,
            ''))


def convert(files,
            outfolder='./3dtiles',
            overwrite=False,
            jobs=multiprocessing.cpu_count(),
            cache_size=int(TOTAL_MEMORY_MB / 10),
            srs_out=None,
            srs_in=None,
            fraction=100,
            benchmark=None,
            rgb=True,
            graph=False,
            color_scale=None,
            verbose=False):
    """convert

    Convert pointclouds (xyz, las or laz) to 3dtiles tileset containing pnts node

    :param files: Filenames to process. The file must use the .las, .laz or .xyz format.
    :type files: list of str, or str
    :param outfolder: The folder where the resulting tileset will be written.
    :type outfolder: path-like object
    :param overwrite: Overwrite the ouput folder if it already exists.
    :type overwrite: bool
    :param jobs: The number of parallel jobs to start. Default to the number of cpu.
    :type jobs: int
    :param cache_size: Cache size in MB. Default to available memory / 10.
    :type cache_size: int
    :param srs_out: SRS to convert the output with (numeric part of the EPSG code)
    :type srs_out: int or str
    :param srs_in: Override input SRS (numeric part of the EPSG code)
    :type srs_in: int or str
    :param fraction: Percentage of the pointcloud to process, between 0 and 100.
    :type fraction: int
    :param benchmark: Print summary at the end of the process
    :type benchmark: str
    :param rgb: Export rgb attributes.
    :type rgb: bool
    :param graph: Produce debug graphes (requires pygal).
    :type graph: bool
    :param color_scale: Force color scale
    :type color_scale: float

    :raises SrsInMissingException: if py3dtiles couldn't find srs informations in input files and srs_in is not specified
    """

    # allow str directly if only one input
    files = [files] if isinstance(files, str) else files

    # read all input files headers and determine the aabb/spacing
    extensions = set()
    for file in files:
        extensions.add(PurePath(file).suffix)
    if len(extensions) != 1:
        raise ValueError("All files should have the same extension, currently there are", extensions)
    extension = extensions.pop()

    init_reader_fn = las_reader.init if extension in ('.las', '.laz') else xyz_reader.init
    infos = init_reader_fn(files, color_scale=color_scale, srs_in=srs_in, srs_out=srs_out)

    avg_min = infos['avg_min']
    rotation_matrix = None
    # srs stuff
    transformer = None
    if srs_out:
        crs_out = CRS('epsg:{}'.format(srs_out))
        if srs_in:
            crs_in = CRS('epsg:{}'.format(srs_in))
        elif not infos['srs_in']:
            raise SrsInMissingException('No SRS information in the provided files')
        else:
            crs_in = CRS(infos['srs_in'])

        transformer = Transformer.from_crs(crs_in, crs_out)

        bl = np.array(list(transformer.transform(
            infos['aabb'][0][0], infos['aabb'][0][1], infos['aabb'][0][2])))
        tr = np.array(list(transformer.transform(
            infos['aabb'][1][0], infos['aabb'][1][1], infos['aabb'][1][2])))
        br = np.array(list(transformer.transform(
            infos['aabb'][1][0], infos['aabb'][0][1], infos['aabb'][0][2])))

        avg_min = np.array(list(transformer.transform(
            avg_min[0], avg_min[1], avg_min[2])))

        x_axis = br - bl

        bl = bl - avg_min
        tr = tr - avg_min

        if srs_out == '4978':
            # Transform geocentric normal => (0, 0, 1)
            # and 4978-bbox x axis => (1, 0, 0),
            # to have a bbox in local coordinates that's nicely aligned with the data
            rotation_matrix = make_rotation_matrix(avg_min, np.array([0, 0, 1]))
            rotation_matrix = np.dot(
                make_rotation_matrix(x_axis, np.array([1, 0, 0])),
                rotation_matrix)

            bl = np.dot(bl, rotation_matrix[:3, :3].T)
            tr = np.dot(tr, rotation_matrix[:3, :3].T)

        root_aabb = np.array([
            np.minimum(bl, tr),
            np.maximum(bl, tr)
        ])
    else:
        # offset
        root_aabb = infos['aabb'] - avg_min

    original_aabb = root_aabb

    base_spacing = compute_spacing(root_aabb)
    if base_spacing > 10:
        root_scale = np.array([0.01, 0.01, 0.01])
    elif base_spacing > 1:
        root_scale = np.array([0.1, 0.1, 0.1])
    else:
        root_scale = np.array([1, 1, 1])

    root_aabb = root_aabb * root_scale
    root_spacing = compute_spacing(root_aabb)

    octree_metadata = OctreeMetadata(aabb=root_aabb, spacing=root_spacing, scale=root_scale[0])

    # create folder
    out_folder_path = Path(outfolder)
    if out_folder_path.is_dir():
        if overwrite:
            shutil.rmtree(out_folder_path, ignore_errors=True)
        else:
            print(f"Error, folder '{outfolder}' already exists")
            sys.exit(1)

    out_folder_path.mkdir()
    working_dir = out_folder_path / "tmp"
    working_dir.mkdir(parents=True)

    node_store = SharedNodeStore(str(working_dir))

    if verbose >= 1:
        print('Summary:')
        print('  - points to process: {}'.format(infos['point_count']))
        print('  - offset to use: {}'.format(avg_min))
        print('  - root spacing: {}'.format(root_spacing / root_scale[0]))
        print('  - root aabb: {}'.format(root_aabb))
        print('  - original aabb: {}'.format(original_aabb))
        print('  - scale: {}'.format(root_scale))

    startup = time.time()

    initial_portion_count = len(infos['portions'])

    if graph:
        progression_log = open('progression.csv', 'w')

    state = State(infos['portions'], max(1, jobs // 2))

    # zmq setup
    zmq_manager = ZmqManager(jobs, (graph, transformer, octree_metadata, outfolder, rgb, verbose))

    while not zmq_manager.are_all_processes_killed():
        now = time.time() - startup
        at_least_one_job_ended = False

        all_processes_busy = not zmq_manager.can_queue_more_jobs()
        while all_processes_busy or zmq_manager.socket.poll(timeout=0, flags=zmq.POLLIN):
            # Blocking read but it's fine because either all our child processes are busy
            # or we know that there's something to read (zmq.POLLIN)
            start = time.time()
            message = zmq_manager.socket.recv_multipart()

            client_id = message[0]
            result = message[1:]
            return_type = result[0]

            if return_type == ResponseType.IDLE.value:
                zmq_manager.add_idle_client(client_id)

                if all_processes_busy:
                    zmq_manager.time_waiting_an_idle_process += time.time() - start
                all_processes_busy = False

            elif return_type == ResponseType.HALTED.value:
                zmq_manager.number_processes_killed += 1
                all_processes_busy = False

            elif return_type == ResponseType.READ.value:
                state.number_of_reading_jobs -= 1
                at_least_one_job_ended = True

            elif return_type == ResponseType.PROCESSED.value:
                content = pickle.loads(result[-1])
                state.processed_points += content['total']
                state.points_in_progress -= content['total']

                del state.processing_nodes[content['name']]

                if content['name']:
                    node_store.put(content['name'], content['save'])
                    state.waiting_writing_nodes.append(content['name'])

                    if state.is_reading_finish():
                        # if all nodes aren't processed yet,
                        # we should check if linked ancestors are processed
                        if state.processing_nodes or state.node_to_process:
                            finished_node = content['name']
                            if can_pnts_be_written(
                                finished_node, finished_node,
                                state.node_to_process, state.processing_nodes
                            ):
                                state.waiting_writing_nodes.pop(-1)
                                state.pnts_to_writing.append(finished_node)

                                for i in range(len(state.waiting_writing_nodes) - 1, -1, -1):
                                    candidate = state.waiting_writing_nodes[i]

                                    if can_pnts_be_written(
                                        candidate, finished_node,
                                        state.node_to_process, state.processing_nodes
                                    ):
                                        state.waiting_writing_nodes.pop(i)
                                        state.pnts_to_writing.append(candidate)

                        else:
                            for c in state.waiting_writing_nodes:
                                state.pnts_to_writing.append(c)
                            state.waiting_writing_nodes.clear()

                at_least_one_job_ended = True

            elif return_type == ResponseType.PNTS_WRITTEN.value:
                state.points_in_pnts += struct.unpack('>I', result[1])[0]
                state.number_of_writing_jobs -= 1

            elif return_type == ResponseType.NEW_TASK.value:
                count = struct.unpack('>I', result[3])[0]
                state.add_tasks_to_process(result[1], result[2], count)

            else:
                raise NotImplementedError(f"The command {return_type} is not implemented")

        while state.pnts_to_writing and zmq_manager.can_queue_more_jobs():
            node_name = state.pnts_to_writing.pop()
            data = node_store.get(node_name)
            if not data:
                raise ValueError(f'{node_name} has no data')

            zmq_manager.send_to_process([CommandType.WRITE_PNTS.value, node_name, data])
            node_store.remove(node_name)
            state.number_of_writing_jobs += 1

        if zmq_manager.can_queue_more_jobs():
            potentials = sorted(
                # a key (=task) can be in node_to_process and processing_nodes if the node isn't completely processed
                [(k, v) for k, v in state.node_to_process.items() if k not in state.processing_nodes],
                key=lambda f: -len(f[0]))

            while zmq_manager.can_queue_more_jobs() and potentials:
                target_count = 100_000
                job_list = []
                count = 0
                idx = len(potentials) - 1
                while count < target_count and idx >= 0:
                    name, (tasks, point_count) = potentials[idx]
                    count += point_count
                    job_list += [
                        name,
                        node_store.get(name),
                        struct.pack('>I', len(tasks)),
                    ] + tasks
                    del potentials[idx]

                    del state.node_to_process[name]
                    state.processing_nodes[name] = (len(tasks), point_count, now)

                    if name in state.waiting_writing_nodes:
                        state.waiting_writing_nodes.pop(state.waiting_writing_nodes.index(name))
                    idx -= 1

                if job_list:
                    zmq_manager.send_to_process([CommandType.PROCESS_JOBS.value] + job_list)

        while state.can_add_reading_jobs() and zmq_manager.can_queue_more_jobs():
            if verbose >= 1:
                print(f'Submit next portion {state.point_cloud_file_parts[-1]}')
            file, portion = state.point_cloud_file_parts.pop()
            state.points_in_progress += portion[1] - portion[0]

            zmq_manager.send_to_process([CommandType.READ_FILE.value, pickle.dumps({
                'filename': file,
                'offset_scale': (
                    -avg_min,
                    root_scale,
                    rotation_matrix[:3, :3].T if rotation_matrix is not None else None,
                    infos['color_scale'].get(file) if infos['color_scale'] is not None else None,
                ),
                'portion': portion,
            })])

            state.number_of_reading_jobs += 1

        # if at this point we have no work in progress => we're done
        if zmq_manager.are_all_processes_idle() and not zmq_manager.killing_processes:
            zmq_manager.kill_all_processes()

        if at_least_one_job_ended:
            if verbose >= 3:
                print('{:^16}|{:^8}|{:^8}'.format('Name', 'Points', 'Seconds'))
                for name, v in state.processing_nodes.items():
                    print('{:^16}|{:^8}|{:^8}'.format(
                        '{} ({})'.format(name.decode('ascii'), v[0]),
                        v[1],
                        round(now - v[2], 1)))
                print('')
                print('Pending:')
                print('  - root: {} / {}'.format(
                    len(state.point_cloud_file_parts),
                    initial_portion_count))
                print('  - other: {} files for {} nodes'.format(
                    sum([len(f[0]) for f in state.node_to_process.values()]),
                    len(state.node_to_process)))
                print('')
            elif verbose >= 2:
                state.print_debug()
            if verbose >= 1:
                print('{} % points in {} sec [{} tasks, {} nodes, {} wip]'.format(
                    round(100 * state.processed_points / infos['point_count'], 2),
                    round(now, 1),
                    jobs - len(zmq_manager.idle_clients),
                    len(state.processing_nodes),
                    state.points_in_progress))
            elif verbose >= 0:
                percent = round(100 * state.processed_points / infos['point_count'], 2)
                time_left = (100 - percent) * now / (percent + 0.001)
                print('\r{:>6} % in {} sec [est. time left: {} sec]'.format(percent, round(now), round(time_left)), end='', flush=True)

            if graph:
                percent = round(100 * state.processed_points / infos['point_count'], 3)
                print('{}, {}'.format(time.time() - startup, percent), file=progression_log)

        node_store.control_memory_usage(cache_size, verbose)

    if state.points_in_pnts != infos['point_count']:
        raise ValueError("!!! Invalid point count in the written .pnts"
                         + f"(expected: {infos['point_count']}, was: {state.points_in_pnts})")
    if verbose >= 1:
        print('Writing 3dtiles {}'.format(infos['avg_min']))

    write_tileset(outfolder, octree_metadata, avg_min, root_scale, rotation_matrix, rgb)
    shutil.rmtree(working_dir)

    if verbose >= 1:
        print('Done')

    if benchmark:
        print('{},{},{},{}'.format(
            benchmark,
            ','.join([os.path.basename(f) for f in files]),
            state.points_in_pnts,
            round(time.time() - startup, 1)))

    zmq_manager.terminate_all_processes()

    if verbose >= 1:
        print('destroy', round(zmq_manager.time_waiting_an_idle_process, 2))

    if graph:
        progression_log.close()

    # pygal chart
    if graph:
        import pygal

        dateline = pygal.XY(x_label_rotation=25, secondary_range=(0, 100))
        for pid in zmq_manager.activities:
            activity = []
            filename = 'activity.{}.csv'.format(pid)
            i = len(zmq_manager.activities) - zmq_manager.activities.index(pid) - 1
            # activities.index(pid) =
            with open(filename, 'r') as f:
                content = f.read().split('\n')
                for line in content[1:]:
                    line = line.split(',')
                    if line[0]:
                        ts = float(line[0])
                        value = int(line[1]) / 3.0
                        activity.append((ts, i + value * 0.9))

            os.remove(filename)
            if activity:
                activity.append((activity[-1][0], activity[0][1]))
                activity.append(activity[0])
                dateline.add(str(pid), activity, show_dots=False, fill=True)

        with open('progression.csv', 'r') as f:
            values = []
            for line in f.read().split('\n'):
                if line:
                    line = line.split(',')
                    values += [(float(line[0]), float(line[1]))]
        os.remove('progression.csv')
        dateline.add('progression', values, show_dots=False, secondary=True, stroke_style={'width': 2, 'color': 'black'})

        dateline.render_to_file('activity.svg')

    zmq_manager.context.destroy()


def init_parser(subparser, str2bool):

    parser = subparser.add_parser(
        'convert',
        help='Convert .las files to a 3dtiles tileset.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        'files',
        nargs='+',
        help='Filenames to process. The file must use the .las, .laz (lastools must be installed) or .xyz format.')
    parser.add_argument(
        '--out',
        type=str,
        help='The folder where the resulting tileset will be written.',
        default='./3dtiles')
    parser.add_argument(
        '--overwrite',
        help='Delete and recreate the ouput folder if it already exists. WARNING: be careful, there will be no confirmation!',
        default=False,
        type=str2bool)
    parser.add_argument(
        '--jobs',
        help='The number of parallel jobs to start. Default to the number of cpu.',
        default=multiprocessing.cpu_count(),
        type=int)
    parser.add_argument(
        '--cache_size',
        help='Cache size in MB. Default to available memory / 10.',
        default=int(TOTAL_MEMORY_MB / 10),
        type=int)
    parser.add_argument(
        '--srs_out', help='SRS to convert the output with (numeric part of the EPSG code)', type=str)
    parser.add_argument(
        '--srs_in', help='Override input SRS (numeric part of the EPSG code)', type=str)
    parser.add_argument(
        '--fraction',
        help='Percentage of the pointcloud to process.',
        default=100, type=int)
    parser.add_argument(
        '--benchmark',
        help='Print summary at the end of the process', type=str)
    parser.add_argument(
        '--rgb',
        help='Export rgb attributes', type=str2bool, default=True)
    parser.add_argument(
        '--graph',
        help='Produce debug graphes (requires pygal)', type=str2bool, default=False)
    parser.add_argument(
        '--color_scale',
        help='Force color scale', type=float)


def main(args):
    try:
        return convert(args.files,
                       outfolder=args.out,
                       overwrite=args.overwrite,
                       jobs=args.jobs,
                       cache_size=args.cache_size,
                       srs_out=args.srs_out,
                       srs_in=args.srs_in,
                       fraction=args.fraction,
                       benchmark=args.benchmark,
                       rgb=args.rgb,
                       graph=args.graph,
                       color_scale=args.color_scale,
                       verbose=args.verbose)
    except SrsInMissingException:
        print('No SRS information in input files, you should specify it with --srs_in')
        sys.exit(1)
