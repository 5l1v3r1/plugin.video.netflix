# -*- coding: utf-8 -*-
"""
    Copyright (C) 2017 Sebastian Golasch (plugin.video.netflix)
    Copyright (C) 2018 Caphm (original implementation module)
    Copyright (C) 2019 Stefano Gottardo - @CastagnaIT
    Kodi library integration: task management

    SPDX-License-Identifier: MIT
    See LICENSES/MIT.md for more information.
"""
from __future__ import absolute_import, division, unicode_literals

from future.utils import iteritems

import os
import re

import resources.lib.common as common
import resources.lib.kodi.nfo as nfo
from resources.lib.api.exceptions import MetadataNotAvailable
from resources.lib.database.db_utils import VidLibProp
from resources.lib.globals import g
from resources.lib.kodi import ui
from resources.lib.kodi.ui import show_library_task_errors
from resources.lib.kodi.library_items import LibraryItems
from resources.lib.kodi.library_utils import (get_episode_title_from_path, get_library_path,
                                              ILLEGAL_CHARACTERS, FOLDER_NAME_MOVIES, FOLDER_NAME_SHOWS)


class LibraryTasks(LibraryItems):

    def execute_library_tasks(self, videoid, task_handlers, nfo_settings=None, notify_errors=False):
        """
        Execute library tasks for a videoid
        :param videoid: the videoid
        :param task_handlers: list of task handler for the operations to do
        :param nfo_settings: the NFOSettings object containing the user's NFO settings
        :param notify_errors: if True a dialog box will be displayed at each error
        """
        list_errors = []
        index = 0
        # Preparation of compiled tasks
        compiled_tasks = {}
        for task_handler in task_handlers:
            compiled_tasks[task_handler] = self.compile_tasks(videoid, task_handler, nfo_settings)
        total_tasks = sum(len(list_tasks) for list_tasks in compiled_tasks.values())
        # Execute the tasks
        for task_handler, compiled_tasks in iteritems(compiled_tasks):
            for compiled_task in compiled_tasks:
                self._execute_task(task_handler, compiled_task, list_errors)
                index += 1
                yield index, total_tasks, compiled_task['title']
        show_library_task_errors(notify_errors, list_errors)

    def execute_library_tasks_gui(self, videoid, task_handlers, title, nfo_settings=None, show_prg_dialog=True):
        """
        Execute library tasks for a videoid, by showing a GUI progress bar/dialog
        :param videoid: the videoid
        :param task_handlers: list of task handler for the operations to do
        :param title: title for the progress dialog/background progress bar
        :param nfo_settings: the NFOSettings object containing the user's NFO settings
        :param show_prg_dialog: if True show progress dialog, otherwise, a background progress bar
        """
        list_errors = []
        # Preparation of compiled tasks
        compiled_tasks = {}
        for task_handler in task_handlers:
            compiled_tasks[task_handler] = self.compile_tasks(videoid, task_handler, nfo_settings)
        total_tasks = sum(len(list_tasks) for list_tasks in compiled_tasks.values())
        # Set a progress bar
        progress_class = ui.ProgressDialog if show_prg_dialog else ui.ProgressBarBG
        with progress_class(show_prg_dialog, title, total_tasks) as progress_bar:
            progress_bar.set_wait_message()
            # Execute the tasks
            for task_handler, compiled_tasks in iteritems(compiled_tasks):
                for compiled_task in compiled_tasks:
                    self._execute_task(task_handler, compiled_task, list_errors)
                    progress_bar.perform_step()
                    progress_bar.set_message('{} ({}/{})'.format(compiled_task['title'],
                                                                 progress_bar.value,
                                                                 progress_bar.max_value))
        show_library_task_errors(show_prg_dialog, list_errors)

    def _execute_task(self, task_handler, compiled_task, list_errors):
        if not compiled_task:  # No metadata or unexpected task compiling behaviour
            return
        try:
            task_handler(compiled_task, get_library_path())
        except Exception as exc:  # pylint: disable=broad-except
            import traceback
            common.error(g.py2_decode(traceback.format_exc(), 'latin-1'))
            common.error('{} of {} failed', task_handler.__name__, compiled_task['title'])
            list_errors.append({
                'task_title': compiled_task['title'],
                'error': '{}: {}'.format(type(exc).__name__, exc)})

    @common.time_execution(immediate=True)
    def compile_tasks(self, videoid, task_handler, nfo_settings=None):
        """Compile a list of tasks for items based on the videoid"""
        common.debug('Compiling library tasks for task handler "{}" and videoid "{}"', task_handler.__name__, videoid)
        tasks = None
        try:
            if task_handler == self.export_item:
                metadata = self.ext_func_get_metadata(videoid)  # pylint: disable=not-callable
                if videoid.mediatype == common.VideoId.MOVIE:
                    tasks = self._create_export_movie_task(videoid, metadata[0], nfo_settings)
                if videoid.mediatype in common.VideoId.TV_TYPES:
                    tasks = self._create_export_tv_tasks(videoid, metadata, nfo_settings)

            if task_handler == self.export_new_item:
                metadata = self.ext_func_get_metadata(videoid, True)  # pylint: disable=not-callable
                tasks = self._create_new_episodes_tasks(videoid, metadata, nfo_settings)

            if task_handler == self.remove_item:
                if videoid.mediatype == common.VideoId.MOVIE:
                    tasks = self._create_remove_movie_task(videoid)
                if videoid.mediatype == common.VideoId.SHOW:
                    tasks = self._compile_remove_tvshow_tasks(videoid)
                if videoid.mediatype == common.VideoId.SEASON:
                    tasks = self._compile_remove_season_tasks(videoid)
                if videoid.mediatype == common.VideoId.EPISODE:
                    tasks = self._create_remove_episode_task(videoid)
        except MetadataNotAvailable:
            common.warn('compile_tasks: unavailable metadata for videoid "{}", tasks compiling skipped', videoid)
            return None
        if tasks is None:
            common.error('compile_tasks: unexpected format for task handler "{}" videoid "{}", tasks compiling skipped',
                         task_handler.__name__, videoid)
        return tasks

    def _create_export_movie_task(self, videoid, movie, nfo_settings):
        """Create a task for a movie"""
        # Reset NFO export to false if we never want movies nfo
        filename = '{title} ({year})'.format(title=movie['title'], year=movie['year'])
        create_nfo_file = nfo_settings and nfo_settings.export_movie_enabled
        nfo_data = nfo.create_movie_nfo(movie) if create_nfo_file else None
        return [self._create_export_item_task(True, create_nfo_file,
                                              videoid=videoid, title=movie['title'],
                                              root_folder_name=FOLDER_NAME_MOVIES,
                                              folder_name=filename,
                                              filename=filename,
                                              nfo_data=nfo_data)]

    def _create_export_tv_tasks(self, videoid, metadata, nfo_settings):
        """Create tasks for a show, season or episode.
        If videoid represents a show or season, tasks will be generated for
        all contained seasons and episodes"""
        if videoid.mediatype == common.VideoId.SHOW:
            tasks = self._compile_export_show_tasks(videoid, metadata[0], nfo_settings)
        elif videoid.mediatype == common.VideoId.SEASON:
            tasks = self._compile_export_season_tasks(videoid,
                                                      metadata[0],
                                                      common.find(int(videoid.seasonid),
                                                                  'id',
                                                                  metadata[0]['seasons']),
                                                      nfo_settings)
        else:
            tasks = [self._create_export_episode_task(videoid, *metadata, nfo_settings=nfo_settings)]

        if nfo_settings and nfo_settings.export_full_tvshow:
            # Create tvshow.nfo file
            # In episode metadata, show data is at 3rd position,
            # while it's at first position in show metadata.
            # Best is to enumerate values to find the correct key position
            key_index = -1
            for i, item in enumerate(metadata):
                if item and item.get('type', None) == 'show':
                    key_index = i
            if key_index > -1:
                tasks.append(self._create_export_item_task(False, True,
                                                           videoid=videoid, title='tvshow.nfo',
                                                           root_folder_name=FOLDER_NAME_SHOWS,
                                                           folder_name=metadata[key_index]['title'],
                                                           filename='tvshow',
                                                           nfo_data=nfo.create_show_nfo(metadata[key_index])))
        return tasks

    def _compile_export_show_tasks(self, videoid, show, nfo_settings):
        """Compile a list of task items for all episodes of all seasons of a tvshow"""
        tasks = []
        for season in show['seasons']:
            tasks += self._compile_export_season_tasks(videoid.derive_season(season['id']), show, season, nfo_settings)
        return tasks

    def _compile_export_season_tasks(self, videoid, show, season, nfo_settings):
        """Compile a list of task items for all episodes in a season"""
        return [self._create_export_episode_task(videoid.derive_episode(episode['id']),
                                                 episode, season, show, nfo_settings)
                for episode in season['episodes']]

    def _create_export_episode_task(self, videoid, episode, season, show, nfo_settings):
        """Export a single episode to the library"""
        filename = 'S{:02d}E{:02d}'.format(season['seq'], episode['seq'])
        title = ' - '.join((show['title'], filename))
        create_nfo_file = nfo_settings and nfo_settings.export_tvshow_enabled
        nfo_data = nfo.create_episode_nfo(episode, season, show) if create_nfo_file else None
        return self._create_export_item_task(True, create_nfo_file,
                                             videoid=videoid, title=title,
                                             root_folder_name=FOLDER_NAME_SHOWS,
                                             folder_name=show['title'],
                                             filename=filename,
                                             nfo_data=nfo_data)

    def _create_export_item_task(self, create_strm_file, create_nfo_file, **kwargs):
        """Create a single task item"""
        return {
            'create_strm_file': create_strm_file,  # True/False
            'create_nfo_file': create_nfo_file,  # True/False
            'videoid': kwargs['videoid'],
            'title': kwargs['title'],  # Progress dialog and debug purpose
            'root_folder_name': kwargs['root_folder_name'],
            'folder_name': re.sub(ILLEGAL_CHARACTERS, '', kwargs['folder_name']),
            'filename': re.sub(ILLEGAL_CHARACTERS, '', kwargs['filename']),
            'nfo_data': kwargs['nfo_data']
        }

    def _create_new_episodes_tasks(self, videoid, metadata, nfo_settings=None):
        tasks = []
        if metadata and 'seasons' in metadata[0]:
            for season in metadata[0]['seasons']:
                if not nfo_settings:
                    nfo_export = g.SHARED_DB.get_tvshow_property(videoid.value, VidLibProp['nfo_export'], False)
                    nfo_settings = nfo.NFOSettings(nfo_export)
                # Check and add missing seasons and episodes
                self._add_missing_items(tasks, season, videoid, metadata, nfo_settings)
        return tasks

    def _add_missing_items(self, tasks, season, videoid, metadata, nfo_settings):
        if g.SHARED_DB.season_id_exists(videoid.value, season['id']):
            # The season exists, try to find any missing episode
            for episode in season['episodes']:
                if not g.SHARED_DB.episode_id_exists(videoid.value, season['id'], episode['id']):
                    tasks.append(self._create_export_episode_task(
                        videoid=videoid.derive_season(season['id']).derive_episode(episode['id']),
                        episode=episode,
                        season=season,
                        show=metadata[0],
                        nfo_settings=nfo_settings
                    ))
                    common.debug('Auto exporting episode {}', episode['id'])
        else:
            # The season does not exist, build task for the season
            tasks += self._compile_export_season_tasks(
                videoid=videoid.derive_season(season['id']),
                show=metadata[0],
                season=season,
                nfo_settings=nfo_settings
            )
            common.debug('Auto exporting season {}', season['id'])

    def _create_remove_movie_task(self, videoid):
        file_path = g.SHARED_DB.get_movie_filepath(videoid.value)
        title = os.path.splitext(os.path.basename(file_path))[0]
        return [self._create_remove_item_task(title, file_path, videoid)]

    def _compile_remove_tvshow_tasks(self, videoid):
        row_results = g.SHARED_DB.get_all_episodes_ids_and_filepath_from_tvshow(videoid.value)
        return self._create_remove_tv_tasks(row_results)

    def _compile_remove_season_tasks(self, videoid):
        row_results = g.SHARED_DB.get_all_episodes_ids_and_filepath_from_season(
            videoid.tvshowid, videoid.seasonid)
        return self._create_remove_tv_tasks(row_results)

    def _create_remove_episode_task(self, videoid):
        file_path = g.SHARED_DB.get_episode_filepath(
            videoid.tvshowid, videoid.seasonid, videoid.episodeid)
        return [self._create_remove_item_task(
            get_episode_title_from_path(file_path),
            file_path, videoid)]

    def _create_remove_tv_tasks(self, row_results):
        return [self._create_remove_item_task(get_episode_title_from_path(row['FilePath']),
                                              row['FilePath'],
                                              common.VideoId.from_dict(
                                                  {'mediatype': common.VideoId.SHOW,
                                                   'tvshowid': row['TvShowID'],
                                                   'seasonid': row['SeasonID'],
                                                   'episodeid': row['EpisodeID']}))
                for row in row_results]

    def _create_remove_item_task(self, title, file_path, videoid):
        """Create a single task item"""
        return {
            'title': title,  # Progress dialog and debug purpose
            'file_path': file_path,
            'videoid': videoid
        }
