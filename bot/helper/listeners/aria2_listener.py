#!/usr/bin/env python3
import asyncio
import time
from typing import Any, Optional

import aioaria2
import humanize
import walrus
from bot import aria2, download_dict_lock, download_dict, LOGGER, config_dict
from bot.helper.ext_utils.task_manager import limit_checker
from bot.helper.mirror_utils.upload_utils.gdriveTools import GoogleDriveHelper
from bot.helper.mirror_utils.status_utils.aria2_status import Aria2Status
from bot.helper.ext_utils.fs_utils import get_base_name, clean_unwanted
from bot.helper.ext_utils.bot_utils import getDownloadByGid, create_task, sync_to_async
from bot.helper.telegram_helper.message_utils import sendMessage, deleteMessage, update_all_messages
from bot.helper.themes import BotTheme
import aiopath
import aiofiles


@create_task
async def on_download_started(api: aioaria2.Aria2, gid: str) -> None:
    """Handle the event when a download is started."""
    download = await api.get_download(gid)
    if download.options.follow_torrent == 'false':
        return
    if download.is_metadata:
        LOGGER.info(f'onDownloadStarted: {gid} METADATA')
        await asyncio.sleep(1)
        if dl := await getDownloadByGid(gid):
            listener = dl.listener()
            if listener.select:
                metamsg = "Downloading Metadata, wait then you can select files. Use torrent file to avoid this wait."
                meta = await sendMessage(listener.message, metamsg)
                async for _ in walrus.wait_for(lambda: download.is_removed or download.followed_by_ids, timeout=2):
                    if download.is_removed or download.followed_by_ids:
                        await deleteMessage(meta)
                        break
                    download = await api.get_download(gid)
        return
    else:
        LOGGER.info(f'onDownloadStarted: {get_base_name(download.name)} - Gid: {gid}')
    dl = None
    if any([config_dict['DIRECT_LIMIT'],
            config_dict['TORRENT_LIMIT'],
            config_dict['LEECH_LIMIT'],
            config_dict['STORAGE_THRESHOLD'],
            config_dict['DAILY_TASK_LIMIT'],
            config_dict['DAILY_MIRROR_LIMIT'],
            config_dict['DAILY_LEECH_LIMIT']]):
        async for _ in walrus.wait_for(lambda: dl is not None, timeout=1):
            if dl is None:
                dl = await getDownloadByGid(gid)
            if dl:
                if not hasattr(dl, 'listener'):
                    LOGGER.warning(
                        f"onDownloadStart: {gid}. at Download limit didn't pass since download completed earlier!")
                    return
                listener = dl.listener()
                download = await api.get_download(gid)
                if not isinstance(download, aioaria2.DownloadTorrent):
                    await asyncio.sleep(3)
                    download = download.live
                size = download.total_length
                LOGGER.info(f"listener size : {humanize.naturalsize(size)}")
                if limit_exceeded := await limit_checker(size, listener):
                    await listener.on_download_error(limit_exceeded)
                    await api.remove([download], force=True, files=True)
    if config_dict['STOP_DUPLICATE']:
        async for _ in walrus.wait_for(lambda: dl is not None, timeout=1):
            if dl is None:
                dl = await getDownloadByGid(gid)
            if dl:
                if not hasattr(dl, 'listener'):
                    LOGGER.warning(
                        f"onDownloadStart: {gid}. STOP_DUPLICATE didn't pass since download completed earlier!")
                    return
                listener = dl.listener()
                if not listener.isLeech and not listener.select and listener.upPath == 'gd':
                    download = await api.get_download(gid)
                    if not isinstance(download, aioaria2.DownloadTorrent):
                        await asyncio.sleep(3)
                        download = download.live
                    LOGGER.info('Checking File/Folder if already in Drive...')
                    name = download.name
                    if listener.compress:
                        name = f"{name}.zip"
                    elif listener.extract:
                        try:
                            name = get_base_name(name)
                        except Exception:
                            name = None
                    if name is not None:
                        telegraph_content, contents_no = await GoogleDriveHelper().drive_list(name, True)
                        if telegraph_content:
                            msg = BotTheme('STOP_DUPLICATE', content=contents_no)
                            button = await get_telegraph_list(telegraph_content)
                            await listener.on_download_error(msg, button)
                            await api.remove([download], force=True, files=True)
                            return


@create_task
async def on_download_complete(api: aioaria2.Aria2, gid: str) -> None:
    """Handle the event when a download is completed."""
    async for _ in walrus.wait_for(lambda: True, timeout=1):
        try:
            download = await api.get_download(gid)
        except Exception:
            return
        if download.options.follow_torrent == 'false':
            return
        if download.followed_by_ids:
            new_gid = download.followed_by_ids[0]
            LOGGER.info(f'Gid changed from {gid} to {new_gid}')
            if dl := await getDownloadByGid(new_gid):
                listener = dl.listener()
                if config_dict['BASE_URL'] and listener.select:
                    if not dl.queued:
                        await api.client.force_pause(new_gid)
                    SBUTTONS = bt_selection_buttons(new_gid)
                    msg = "Your download paused. Choose files then press Done Selecting button to start downloading."
                    await sendMessage(listener.message, msg, SBUTTONS)
        elif download.is_torrent:
            if dl := await getDownloadByGid(gid):
                if hasattr(dl, 'listener') and dl.seeding:
                    LOGGER.info(
                        f"Cancelling Seed: {get_base_name(download.name)} onDownloadComplete")
                    listener = dl.listener()
                    await listener.on_upload_error(f"Seeding stopped with Ratio: {dl.ratio()} and Time: {dl.seeding_time()}")
                    await api.remove([download], force=True, files=True)
        else:
            LOGGER.info(f"onDownloadComplete: {get_base_name(download.name)} - Gid: {gid}")
            if dl := await getDownloadByGid(gid):
                listener = dl.listener()
                await listener.on_download_complete()
                await api.remove([download], force=True, files=True)


@create_task
async def on_bt_download_complete(api: aioaria2.Aria2, gid: str) -> None:
    """Handle the event when a BitTorrent download is completed."""
    seed_start_time = time.time()
    async for _ in walrus.wait_for(lambda: True, timeout=1):
        download = await api.get_download(gid)
        if download.options.follow_torrent == 'false':
            return
        LOGGER.info(f"onBtDownloadComplete: {get_base_name(download.name)} - Gid: {gid}")
        if dl := await getDownloadByGid(gid):
            listener = dl.listener()
            if listener.select:
                res = download.files
                for file_o in res:
                    f_path = file_o.path
                    if not file_o.selected and await aiopath.is_file(f_path):
                        try:
                            await aiofiles.os.remove(f_path)
                        except Exception:
                            pass
                await clean_unwanted(download.dir)
            if listener.seed:
                try:
                    await api.set_options({'max-upload-limit': '0'}, [download])
                except Exception as e:
                    LOGGER.error(
                        f'{e} You are not able to seed because you added global option seed-time=0 without adding specific seed_time for this torrent GID: {gid}')
            else:
                try:
                    await api.client.force_pause(gid)
                except Exception as e:
                    LOGGER.error(f"{e} GID: {gid}")
            await listener.on_download_complete()
            download = download.live
            if listener.seed:
                if download.is_complete:
                    if dl := await getDownloadByGid(gid):
                        LOGGER.info(f"Cancelling Seed: {get_base_name(download.name)}")
                        await listener.on_upload_error(f"Seeding stopped with Ratio: {dl.ratio()} and Time: {dl.seeding_time()}")
                        await api.remove([download], force=True, files=True)
                else:
                    async with download_dict_lock:
                        if listener.uid not in download_dict:
                            await api.remove([download], force=True, files=True)
                            return
                        download_dict[listener.uid] = Aria2Status(
                            gid, listener, True)
                        download_dict[listener.uid].start_time = seed_start_time
                    LOGGER.info(f"Seeding started: {get_base_name(download.name)} - Gid: {gid}")
                    await update_all_messages()
            else:
                await api.remove([download], force=True, files=True)


@create_task
async def on_download_stopped(api: aioaria2.Aria2, gid: str) -> None:
    """Handle the event when a download is stopped."""
    async for _ in walrus.wait_for(lambda: True, timeout=1):
        if dl := await getDownloadByGid(gid):
            listener = dl.listener()
            await listener.on_download_error('Dead torrent!')


@create_task
async def on_download_error(api: aioaria2.Aria2, gid: str) -> None:
    """Handle the event when a download encounters an error."""
    LOGGER.info(f"onDownloadError: {get_base_name(gid)}")
    error = "None"
    try:
        download = await api.get_download(gid)
        if download.options.follow_torrent == 'false':
            return
        error = download.error_message
        LOGGER.info(f"Download Error: {error}")
    except Exception:
        pass
    if dl := await getDownloadByGid(gid):
        listener = dl.listener()
        await listener.on_download_error(error)


async def start_aria2_listener() -> None:
    """Start the aria2 event listener."""
    notifications = aria2.notifications
    coroutines = [on_download_started(aria2, notification.gid) for notification in notifications if notification.type == 'downloadStart']
    coroutines += [on_download_error(aria2, notification.gid) for notification in notifications if notification.type == 'downloadError']
    coroutines += [on_download_stopped(aria2, notification.gid) for notification in notifications if notification.type == 'downloadStop']
    coroutines += [on_download_complete(aria2, notification.gid) for notification in notifications if notification.type == 'downloadComplete']
    coroutines += [on_bt_download_complete(aria2, notification.gid) for notification in notifications if notification.type == 'btDownloadComplete']
    await asyncio.wait(coroutines)
