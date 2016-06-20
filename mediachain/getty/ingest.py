import json
import sys
from copy import deepcopy
from datetime import datetime
from os import walk, path
from grpc.framework.interfaces.face.face import AbortionError

from mediachain.datastore.data_objects import Artefact, Entity, \
    ArtefactCreationCell, MultihashReference
from mediachain.datastore.dynamo import get_db
from mediachain.datastore import get_raw_datastore
from mediachain.transactor.client import TransactorClient
from mediachain.getty.thumbnails import get_thumbnail_data

TRANSLATOR_ID = u'GettyTranslator/0.1'


def ingest(host, port, dir_root, max_num=0, download_thumbs=False):
    transactor = TransactorClient(host, port)
    datastore = get_db()

    try:
        for artefact, artefact_ref, entity_ref, cell_ref in getty_artefacts(
            transactor, datastore, dir_root, max_num, download_thumbs
        ):
            getty_id = artefact.meta[u'data'][u'_id']
            print "Ingested getty image '{0}' {1}".format(
                getty_id, artefact_ref
            )
    except AbortionError as e:
        print("RPC Error: " + str(e))


def getty_to_mediachain_objects(transactor, meta_source, getty_json, entities,
                                thumbnail_ref=None, raw_ref=None):
    if raw_ref is None:
        raw_ref = meta_source

    common_meta = {u'translated_at': unicode(datetime.utcnow().isoformat()),
                   u'translator': TRANSLATOR_ID,
                   u'raw_ref': raw_ref.to_map()}

    artist_name = getty_json['artist']
    if artist_name in entities:
        entity_ref = entities[artist_name]
    else:
        artist_meta = deepcopy(common_meta)
        artist_meta.update({u'data': {u'name': artist_name}})
        entity = Entity(artist_meta, meta_source=meta_source)
        entity_ref = transactor.insert_canonical(entity)

    data = {u'_id': u'getty_' + getty_json['id'],
            u'title': getty_json['title'],
            u'artist': getty_json['artist'],
            u'collection_name': getty_json['collection_name'],
            u'caption': getty_json['caption'],
            u'editorial_source': getty_json['editorial_source'].get('name', None),
            u'keywords':
                [x['text'] for x in getty_json['keywords'] if 'text' in x],
            u'date_created': getty_json['date_created']
            }

    if thumbnail_ref is not None:
        m = MultihashReference.from_base58(thumbnail_ref)
        data[u'thumbnail'] = m.to_map()

    artefact_meta = deepcopy(common_meta)
    artefact_meta.update({u'data': data})

    artefact = Artefact(artefact_meta, meta_source=meta_source)

    artefact_ref = transactor.insert_canonical(artefact)
    creation_cell = ArtefactCreationCell(meta=common_meta,
                                         ref=artefact_ref,
                                         entity=entity_ref,
                                         meta_source=meta_source)

    cell_ref = transactor.update_chain(creation_cell)
    return artefact, artefact_ref, entity_ref, cell_ref


def getty_artefacts(transactor,
                    datastore,
                    dd='getty/json/images',
                    max_num=0,
                    download_thumbs=False):
    entities = dedup_artists(transactor, datastore, dd, max_num)

    for content, file_name in walk_json_dir(dd, max_num):
        raw_ref_str = put_raw_data(content)
        raw_ref = MultihashReference.from_base58(raw_ref_str)
        meta_source = MultihashReference.from_base58(datastore.put(content))

        getty_json = json.loads(content.decode('utf-8'))

        thumbnail_ref = None
        thumbnail_data = get_thumbnail_data(file_name, download=download_thumbs)
        if thumbnail_data is not None:
            thumbnail_ref = put_raw_data(thumbnail_data)
            
        yield getty_to_mediachain_objects(
            transactor, meta_source, getty_json, entities,
            thumbnail_ref=thumbnail_ref,
            raw_ref=raw_ref
        )


def dedup_artists(transactor,
                  datastore,
                  dd='getty/json/images',
                  max_num=0):
    print("deduplicating getty artists.  This may take a while...")
    artist_name_map = {}
    total_parsed = 0
    unique = 0
    for content, _ in walk_json_dir(dd, max_num):
        getty_json = json.loads(content.decode('utf-8'))
        total_parsed += 1
        n = getty_json['artist']
        if n is None or n in artist_name_map:
            continue
        unique += 1
        raw_ref_str = put_raw_data(content)
        artist_name_map[n] = raw_ref_str

        sys.stdout.write('\r')
        sys.stdout.write('Parsed {0} entries, {1} unique artists'.format(
            total_parsed, unique
        ))
        sys.stdout.flush()

    entities = {}
    for n, raw_ref_str in artist_name_map.iteritems():
        raw_ref = MultihashReference.from_base58(raw_ref_str)
        meta = {u'rawRef': raw_ref.to_map(),
                u'translatedAt': unicode(datetime.utcnow().isoformat()),
                u'translator': TRANSLATOR_ID,
                u'data': {u'name': n}}
        entity = Entity(meta, meta_source=raw_ref)
        try:
            ref = transactor.insert_canonical(entity)
            entities[n] = ref
        except AbortionError as e:
            print("RPC error inserting entity: " + str(e))

    print('\ndone deduplicating artists')
    return entities


def walk_json_dir(dd='getty',
                  max_num=0):
    nn = 0
    for dir_name, subdir_list, file_list in walk(dd):
        for fn in file_list:
            ext = path.splitext(fn)[-1]
            if ext.lower() != '.json':
                continue

            nn += 1

            if max_num and (nn + 1 >= max_num):
                print ('ENDING EARLY')
                return

            fn = path.join(dir_name, fn)

            with open(fn, mode='rb') as f:
                try:
                    content = f.read()
                except ValueError:
                    print "couldn't import json from {}".format(fn)
                    continue
            yield content, fn


def put_raw_data(raw):
    datastore = get_raw_datastore()
    # print('adding raw metadata to ipfs...')
    ref = datastore.put(raw)
    # print('raw metadata added. ref: ' + str(ref))
    return ref

