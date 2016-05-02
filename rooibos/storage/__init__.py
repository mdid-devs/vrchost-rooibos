from __future__ import with_statement
from PIL import Image
import StringIO
import logging
import mimetypes
import os
import re
from django.conf import settings
from django.db.models import Q
from django.contrib.auth.models import User
from rooibos.access import filter_by_access, \
    get_effective_permissions_and_restrictions
from rooibos.data.models import Record, standardfield_ids
from models import Media, Storage


mimetypes.init([os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', 'mime.types')
)])


# sort images by area
def _imgsizecmp(x, y):
    if x.width and x.height and y.width and y.height:
        return cmp(x.width * x.height, y.width * y.height)
    if x.width and x.height:
        return 1
    if y.width and y.height:
        return -1
    return 0


def get_media_for_record(record, user=None, passwords={}):
    """
    Returns all media accessible to the user either directly through
    collections or indirectly through presentations.
    A user always must have access to the storage where the media is stored.
    """
    from rooibos.presentation.models import Presentation

    record_id = getattr(record, 'id', record)
    record = Record.filter_one_by_access(user, record_id)

    if not record:
        # Try to get to record through an accessible presentation -
        # own presentations don't count, since it's already established
        # that owner doesn't have access to the record.
        pw_q = Q(
            # Presentation must not have password
            Q(password=None) | Q(password='') |
            # or must know password
            Q(id__in=Presentation.check_passwords(passwords))
        )
        access_q = Q(
            # Must have access to presentation
            id__in=filter_by_access(user, Presentation),
            # and presentation must not be archived
            hidden=False
        )
        accessible_presentations = Presentation.objects.filter(
            pw_q, access_q, items__record__id=record_id)
        # Now get all the presentation owners so we can check if any of them
        # have access to the record
        owners = User.objects.filter(
            id__in=accessible_presentations.values('owner'))
        if not any(
                Record.filter_one_by_access(owner, record_id)
                for owner in owners):
            return Media.objects.none()

    return Media.objects.filter(
        record__id=record_id,
        storage__id__in=filter_by_access(user, Storage),
    )


def get_image_for_record(
        record, user=None, width=100000, height=100000, passwords={},
        crop_to_square=False, force_reprocess=False):
    media = get_media_for_record(record, user, passwords)
    q = Q(mimetype__startswith='image/')
    if settings.FFMPEG_EXECUTABLE:
        # also support video and audio
        q = q | Q(mimetype__startswith='video/') | \
            Q(mimetype__startswith='audio/')
    q = q | Q(mimetype='application/pdf')

    media = media.select_related('storage').filter(q)

    if not media:
        return None
    map(lambda m: m.identify(lazy=True), media)
    media = sorted(media, _imgsizecmp, reverse=True)
    # find matching media
    last = None
    for m in media:
        if m.width > width or m.height > height:
            # Image still larger than given dimensions
            last = m
        elif (m.width == width and m.height <= height) or \
                (m.width <= width and m.height == height):
            # exact match
            break
        else:
            # Now we have a smaller image
            m = last or m
            break

    # m is now equal or larger to requested size, or smaller but closest
    # to the requested size

    # check what user size restrictions are
    restrictions = get_effective_permissions_and_restrictions(
        user, m.storage)[3]
    if restrictions:
        try:
            width = min(width, int(restrictions.get('width', width)))
            height = min(height, int(restrictions.get('height', height)))
        except ValueError:
            logging.exception(
                'Invalid height/width restrictions: %s' % repr(restrictions))

    # see if image needs resizing
    if (m.width > width or m.height > height or m.mimetype != 'image/jpeg' or
            not m.is_local() or force_reprocess):

        def derivative_image(master, width, height):
            if not master.file_exists():
                logging.error(
                    'Image derivative failed for media %d, '
                    'cannot find file "%s"' % (
                        master.id, master.get_absolute_file_path()
                    )
                )
                return None, (None, None)
            from PIL import ImageFile
            ImageFile.MAXBLOCK = 16 * 1024 * 1024
            # Import here to avoid circular reference
            # TODO: need to move all these functions out of __init__.py
            from multimedia import get_image, overlay_image_with_mimetype_icon
            try:
                file = get_image(master)
                image = Image.open(file)
                if crop_to_square:
                    w, h = image.size
                    if w > h:
                        image = image.crop(
                            ((w - h) / 2, 0, (w - h) / 2 + h, h))
                    elif w < h:
                        image = image.crop(
                            (0, (h - w) / 2, w, (h - w) / 2 + w))
                image.thumbnail((width, height), Image.ANTIALIAS)
                image = overlay_image_with_mimetype_icon(
                    image, master.mimetype)
                output = StringIO.StringIO()
                if image.mode != "RGB":
                    image = image.convert("RGB")
                image.save(output, 'JPEG', quality=85, optimize=True)
                return output.getvalue(), image.size
            except Exception, e:
                logging.error(
                    'Image derivative failed for media %d (%s)' %
                    (master.id, e)
                )
                return None, (None, None)

        # See if a derivative already exists
        name = '%s-%sx%s%s.jpg' % (
            m.id, width, height, 'sq' if crop_to_square else '')
        sp = m.storage.get_derivative_storage_path()
        if sp:
            path = os.path.join(sp, name)

            if not os.path.exists(path) or os.path.getsize(path) == 0:
                output, (w, h) = derivative_image(m, width, height)
                if output:
                    with file(path, 'wb') as f:
                        f.write(output)
                else:
                    return None
            return path

        else:
            return None

    else:

        return m.get_absolute_file_path()


def get_thumbnail_for_record(record, user=None, crop_to_square=False):
    return get_image_for_record(
        record, user, width=100, height=100, crop_to_square=crop_to_square)


def find_record_by_identifier(
        identifiers, collection, owner=None,
        ignore_suffix=False, suffix_regex=r'[-_]\d+$'):
    idfields = standardfield_ids('identifier', equiv=True)
    if not isinstance(identifiers, (list, tuple)):
        identifiers = [identifiers]
    else:
        identifiers = list(identifiers)
    if ignore_suffix:
        identifiers.extend(
            [re.sub(suffix_regex, '', id) for id in identifiers])
    records = Record.by_fieldvalue(
        idfields, identifiers).filter(
        collection=collection, owner=owner).distinct()
    return records


def match_up_media(storage, collection, allow_multiple_use=False):
    # While matching up when multiple use is allowed, we want to get all
    # the files that are already in use as well, so they can be matched up
    # again
    _broken, files = analyze_media(
        storage,
        allow_multiple_use,
        remove_used_from_extra=not allow_multiple_use
    )
    # find records that have an ID matching one of the remaining files
    for file in files:
        # Match identifiers that are either full file name (with extension)
        # or just base name match
        filename = os.path.split(file)[1]
        id = os.path.splitext(filename)[0]
        records = find_record_by_identifier(
            (id, filename),
            collection,
            ignore_suffix=True
        ).filter(media=None).distinct()
        if len(records) == 1:
            yield records[0], file


def analyze_records(collection, storage):
    # find empty records, i.e. records that don't have any media in the
    # given storage
    return collection.records.exclude(
        id__in=collection.records.filter(media__storage=storage).values('id'))


def analyze_media(storage, allow_multiple_use=False,
                  remove_used_from_extra=True):
    broken = []
    used = []
    # Storage must be able to provide file list
    if hasattr(storage, 'get_files'):
        # Find extra files, i.e. files in the storage area that don't
        # have a matching media record
        files = storage.get_files()
        # convert to dict for faster lookup
        extra = dict(zip(files, [None] * len(files)))
        # Find broken media, i.e. media that does not have a related file
        # on the file system
        for media in Media.objects.filter(storage=storage):
            url = os.path.normcase(os.path.normpath(media.url))
            if url in extra:
                # File is in use
                if not allow_multiple_use:
                    del extra[url]
                else:
                    used.append(url)
            else:
                # missing file
                broken.append(media)
        if remove_used_from_extra:
            for url in used:
                if url in extra:
                    del extra[url]
        extra = extra.keys()
    return broken, extra
