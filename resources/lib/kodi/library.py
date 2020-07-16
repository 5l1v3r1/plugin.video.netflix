# -*- coding: utf-8 -*-
"""
    Copyright (C) 2017 Sebastian Golasch (plugin.video.netflix)
    Copyright (C) 2018 Caphm (original implementation module)
    Copyright (C) 2020 Stefano Gottardo
    Kodi library integration

    SPDX-License-Identifier: MIT
    See LICENSES/MIT.md for more information.
"""
from __future__ import absolute_import, division, unicode_literals

from datetime import datetime

from future.utils import iteritems

import resources.lib.api.api_requests as api
import resources.lib.common as common
import resources.lib.kodi.nfo as nfo
import resources.lib.kodi.ui as ui
from resources.lib.database.db_utils import VidLibProp
from resources.lib.globals import g
from resources.lib.kodi.library_tasks import LibraryTasks
from resources.lib.kodi.library_utils import (request_kodi_library_upd, get_library_path,
                                              FOLDER_NAME_MOVIES, FOLDER_NAME_SHOWS,
                                              is_auto_update_library_running, request_kodi_library_upd_decorator)
from resources.lib.navigation.directory_utils import delay_anti_ban

try:  # Python 2
    unicode
except NameError:  # Python 3
    unicode = str  # pylint: disable=redefined-builtin

# Reasons that led to the creation of a class for the library operations:
# - Time-consuming update functionality like "full sync of kodi library", "auto update", "export" (large tv show)
#    from context menu or settings, can not be performed within of the service side or will cause IPC timeouts,
#    and could block IPC access for other actions at same time.
# - The scheduled update operations for the library require direct access to nfsession functions,
#    otherwise if you use the IPC callback to access to nfsession will cause the continuous display
#    of the loading screens while using Kodi, then to avoid the loading screen on update
#    is needed run the whole code within the service side.
# - Simple operations as "remove" can be executed directly without use of nfsession/IPC and speed up the operations.
# A class allows you to choice to retrieve the data from netflix API through IPC or directly from nfsession.


def get_library_cls():
    """
    Get the library class to do library operations
    FUNCTION TO BE USED ONLY ON ADD-ON CLIENT INSTANCES
    """
    # This build a instance of library class by assigning access to external functions through IPC
    return Library(api.get_metadata, api.get_mylist_videoids_profile_switch)


class Library(LibraryTasks):
    """Kodi library integration"""

    def __init__(self, func_get_metadata, func_get_mylist_videoids_profile_switch):
        super(Library, self).__init__()
        # External functions
        self.ext_func_get_metadata = func_get_metadata
        self.ext_func_get_mylist_videoids_profile_switch = func_get_mylist_videoids_profile_switch

    @request_kodi_library_upd_decorator
    def export_to_library(self, videoid, show_prg_dialog=True):
        """
        Export an item to the Kodi library
        :param videoid: the videoid
        :param show_prg_dialog: if True show progress dialog, otherwise, a background progress bar
        """
        nfo_settings = nfo.NFOSettings()
        nfo_settings.show_export_dialog(videoid.mediatype)
        self.execute_library_tasks_gui(videoid,
                                       [self.export_item],
                                       title=common.get_local_string(30018),
                                       nfo_settings=nfo_settings,
                                       show_prg_dialog=show_prg_dialog)

    @request_kodi_library_upd_decorator
    def export_to_library_new_episodes(self, videoid, show_prg_dialog=True):
        """
        Export new episodes for a tv show by it's videoid
        :param videoid: The videoid of the tv show to process
        :param show_prg_dialog: if True show progress dialog, otherwise, a background progress bar
        """
        if videoid.mediatype != common.VideoId.SHOW:
            common.debug('{} is not a tv show, no new episodes will be exported', videoid)
            return
        nfo_settings = nfo.NFOSettings()
        nfo_settings.show_export_dialog(videoid.mediatype)
        common.debug('Exporting new episodes for {}', videoid)
        self.execute_library_tasks_gui(videoid,
                                       [self.export_new_item],
                                       title=common.get_local_string(30198),
                                       nfo_settings=nfo_settings,
                                       show_prg_dialog=show_prg_dialog)

    @request_kodi_library_upd_decorator
    def update_library(self, videoid, show_prg_dialog=True):
        """
        Update items in the Kodi library
        :param videoid: the videoid
        :param show_prg_dialog: if True show progress dialog, otherwise, a background progress bar
        """
        nfo_settings = nfo.NFOSettings()
        nfo_settings.show_export_dialog(videoid.mediatype)
        self.execute_library_tasks_gui(videoid,
                                       [self.remove_item, self.export_item],
                                       title=common.get_local_string(30061),
                                       nfo_settings=nfo_settings,
                                       show_prg_dialog=show_prg_dialog)

    def remove_from_library(self, videoid, show_prg_dialog=True):
        """
        Remove an item from the Kodi library
        :param videoid: the videoid
        :param show_prg_dialog: if True show progress dialog, otherwise, a background progress bar
        """
        self.execute_library_tasks_gui(videoid,
                                       [self.remove_item],
                                       title=common.get_local_string(30030),
                                       show_prg_dialog=show_prg_dialog)

    def sync_library_with_mylist(self):
        """
        Perform a full sync of Kodi library with Netflix "My List",
        by deleting everything that was previously exported
        """
        common.info('Performing sync of Kodi library with My list')
        # Clear all the library
        self.clear_library()
        # Start the sync
        self.auto_update_library(True, show_nfo_dialog=True, clear_on_cancel=True)

    @common.time_execution(immediate=True)
    def clear_library(self, show_prg_dialog=True):
        """
        Delete all exported items to Kodi library, clean the add-on database, clean the folders
        :param show_prg_dialog: if True, will be show a progress dialog window
        """
        common.info('Start deleting exported library items')
        with ui.ProgressDialog(show_prg_dialog, common.get_local_string(30500), 3) as progress_dlg:
            progress_dlg.perform_step()
            progress_dlg.set_wait_message()
            g.SHARED_DB.purge_library()
            for folder_name in [FOLDER_NAME_MOVIES, FOLDER_NAME_SHOWS]:
                progress_dlg.perform_step()
                progress_dlg.set_wait_message()
                section_root_dir = common.join_folders_paths(get_library_path(), folder_name)
                common.delete_folder_contents(section_root_dir, delete_subfolders=True)
        # Update Kodi library database
        common.clean_library()

    def auto_update_library(self, sync_with_mylist, show_prg_dialog=True, show_nfo_dialog=False, clear_on_cancel=False):
        """
        Perform an auto update of the exported items in to Kodi library.
        - The main purpose is check if there are new seasons/episodes.
        - In the case "Sync Kodi library with My list" feature is enabled, will be also synchronized with My List.
        :param sync_with_mylist: if True, sync the Kodi library with Netflix My List
        :param show_prg_dialog: if True, will be show a progress dialog window and the errors will be notified to user
        :param show_nfo_dialog: if True, ask to user if want export NFO files (override custom NFO actions for videoid)
        :param clear_on_cancel: if True, when cancel the operations will be cleared the entire library
        """
        if is_auto_update_library_running():
            return
        common.info('Start auto-updating of Kodi library {}',
                    '(with sync of My List)' if sync_with_mylist else '')
        g.SHARED_DB.set_value('library_auto_update_is_running', True)
        g.SHARED_DB.set_value('library_auto_update_start_time', datetime.now())
        try:
            # Get the full list of the exported tvshows/movies as id (VideoId.value)
            exp_tvshows_videoids_values = g.SHARED_DB.get_tvshows_id_list()
            exp_movies_videoids_values = g.SHARED_DB.get_movies_id_list()

            # Get the exported tvshows (to be updated) as dict (key=videoid, value=type of task)
            videoids_tasks = {
                common.VideoId.from_path([common.VideoId.SHOW, videoid_value]): self.export_new_item
                for videoid_value in g.SHARED_DB.get_tvshows_id_list(VidLibProp['exclude_update'], False)
            }

            if sync_with_mylist:
                # Get My List videoids of the chosen profile
                # pylint: disable=not-callable
                mylist_video_id_list, mylist_video_id_list_type = self.ext_func_get_mylist_videoids_profile_switch()

                # Check if tv shows have been removed from the My List
                for videoid_value in exp_tvshows_videoids_values:
                    if unicode(videoid_value) in mylist_video_id_list:
                        continue
                    # The tv show no more exist in My List so remove it from library
                    videoid = common.VideoId.from_path([common.VideoId.SHOW, videoid_value])
                    videoids_tasks.update({videoid: self.remove_item})

                # Check if movies have been removed from the My List
                for videoid_value in exp_movies_videoids_values:
                    if unicode(videoid_value) in mylist_video_id_list:
                        continue
                    # The movie no more exist in My List so remove it from library
                    videoid = common.VideoId.from_path([common.VideoId.MOVIE, videoid_value])
                    videoids_tasks.update({videoid: self.remove_item})

                # Add to library the missing tv shows / movies of My List
                for index, videoid_value in enumerate(mylist_video_id_list):
                    if (int(videoid_value) not in exp_tvshows_videoids_values and
                            int(videoid_value) not in exp_movies_videoids_values):
                        is_movie = mylist_video_id_list_type[index] == 'movie'
                        videoid = common.VideoId(**{('movieid' if is_movie else 'tvshowid'): videoid_value})
                        videoids_tasks.update({videoid: self.export_new_item if is_movie else self.export_item})

            # Start the update operations
            ret = self._update_library(videoids_tasks, exp_tvshows_videoids_values, show_prg_dialog, show_nfo_dialog,
                                       clear_on_cancel)
            g.SHARED_DB.set_value('library_auto_update_is_running', False)
            if not ret:
                common.warn('Auto update of the Kodi library interrupted')
                return
            request_kodi_library_upd()
            common.info('Auto update of the Kodi library completed')
            if not g.ADDON.getSettingBool('lib_auto_upd_disable_notification'):
                ui.show_notification(common.get_local_string(30220), time=5000)
        except Exception as exc:  # pylint: disable=broad-except
            import traceback
            common.error('An error has occurred in the library auto update: {}', exc)
            common.error(g.py2_decode(traceback.format_exc(), 'latin-1'))
            g.SHARED_DB.set_value('library_auto_update_is_running', False)

    def _update_library(self, videoids_tasks, exp_tvshows_videoids_values, show_prg_dialog, show_nfo_dialog,
                        clear_on_cancel):
        # If set ask to user if want to export NFO files (override user custom NFO settings for videoids)
        nfo_settings_override = None
        if show_nfo_dialog:
            nfo_settings_override = nfo.NFOSettings()
            nfo_settings_override.show_export_dialog()
        # Get the exported tvshows, but to be excluded from the updates
        excluded_videoids_values = g.SHARED_DB.get_tvshows_id_list(VidLibProp['exclude_update'], True)
        # Start the update operations
        with ui.ProgressDialog(show_prg_dialog, max_value=len(videoids_tasks)) as progress_bar:
            for videoid, task_handler in iteritems(videoids_tasks):
                # Check if current videoid is excluded from updates
                if int(videoid.value) in excluded_videoids_values:
                    continue
                # Get the NFO settings for the current videoid
                if not nfo_settings_override and int(videoid.value) in exp_tvshows_videoids_values:
                    # User custom NFO setting
                    # it is possible that the user has chosen not to export NFO files for a specific tv show
                    nfo_export = g.SHARED_DB.get_tvshow_property(videoid.value,
                                                                 VidLibProp['nfo_export'], False)
                    nfo_settings = nfo.NFOSettings(nfo_export)
                else:
                    nfo_settings = nfo_settings_override or nfo.NFOSettings()
                # Execute the task
                for index, total_tasks, title in self.execute_library_tasks(videoid,
                                                                            [task_handler],
                                                                            nfo_settings=nfo_settings,
                                                                            notify_errors=show_prg_dialog):
                    label_partial_op = ' ({}/{})'.format(index + 1, total_tasks) if total_tasks > 1 else ''
                    progress_bar.set_message(title + label_partial_op)
                if progress_bar.iscanceled():
                    if clear_on_cancel:
                        self.clear_library(show_prg_dialog)
                        return False
                if self.is_abort_requested:
                    return False
                progress_bar.perform_step()
                progress_bar.set_wait_message()
                delay_anti_ban()
        return True

    def import_library(self, is_old_format):
        """
        Imports an already existing library into the add-on library database,
        allows you to recover an existing library, avoiding to recreate it from scratch.
        :param is_old_format: if True, imports library items with old format version (add-on version 13.x)
        """
        nfo_settings = nfo.NFOSettings()
        if is_old_format:
            # TODO-------------------------------------------------------------------------------------------------------
            # for videoid in self.imports_videoids_from_existing_old_library():
            #     self.execute_library_tasks(videoid,
            #                                [self.export_item],
            #                                nfo_settings=nfo_settings,
            #                                title=common.get_local_string(30018))
            if self.is_abort_requested:
                common.warn('Import library interrupted by Kodi')
                return
            # Here delay_anti_ban is not needed metadata are already cached
        else:
            raise NotImplementedError
