import configargparse
import os
import sys
import json
from common.es_proxy import add_basic_es_arguments, get_es_connection
from common.log import setup_logging
from elasticsearch import TransportError
from elasticsearch.helpers import bulk


# -----------------------------------------------------------------------------
# Original Functions
# -----------------------------------------------------------------------------

def update_indexes_in_alias(es, logger, alias, backup_index_name, index_name):
    ''' cycle alias from backup if it exists, or add index,
        or add index and alias'''
    if es.indices.exists_alias(name=alias):
        if es.indices.exists_alias(name=alias, index=backup_index_name):
            logger.info("Removing from alias %s index %s and adding %s" %
                        (alias, backup_index_name, index_name))
            es.indices.update_aliases(body={
                "actions": [
                    {"remove": {"index": backup_index_name, "alias": alias}},
                    {"add": {"index": index_name, "alias": alias}}
                ]
            })
        else:
            logger.info("Alias %s exists so just add index %s to it" %
                        (alias, index_name))
            es.indices.update_aliases(body={
                "actions": [
                    {"add": {"index": index_name, "alias": alias}}
                ]
            })
    else:
        logger.info("Adding alias %s for index %s" % (alias, index_name))
        es.indices.put_alias(name=alias, index=index_name)


def swap_backup_index(es, logger, alias, index_name, backup_index_name):
    if es.indices.exists_alias(name=alias):
        if es.indices.exists_alias(name=alias, index=index_name):
            logger.info(
                "Alias Exists for index %s.\nSwitiching to backup index %s."
                % (index_name, backup_index_name)
            )
            backup_index_name, index_name = index_name, backup_index_name
        else:
            logger.info(
                "Alias Exists for index %s.\nSwitiching to backup index %s."
                % (backup_index_name, index_name)
            )
    return index_name, backup_index_name


def load_json(logger, file):
    with open(file) as settings_file:
        try:
            jo = json.load(settings_file)
        except Exception:
            logger.error("Warning: file %s is in invalid json format.", file)
            sys.exit(-1)
    return jo


def data_load_strategy_complaint(data):
    with open(data) as f:
        for line in f.readlines():
            doc = json.loads(line)
            if 'complaint_id' not in doc:
                doc['complaint_id'] = doc['public_id']
            yield {'_op_type': 'create',
                   '_id': doc['complaint_id'],
                   '_source': doc}


def yield_chunked_docs(get_data_function, data, chunk_size):
    count = 0
    doc_ary = []
    for doc in get_data_function(data):
        doc_ary.append(doc)
        count += 1
        if count == chunk_size:
            yield doc_ary
            doc_ary = []
            count = 0
    yield doc_ary


def index_json_data(
    es, logger, doc_type_name, settings_json, mapping_json, data, index_name,
    backup_index_name, alias, chunk_size=20000
):
    settings = load_json(logger, settings_json)
    mapping = load_json(logger, mapping_json)

    es.indices.create(index=index_name, ignore=400)
    es.indices.create(index=backup_index_name, ignore=400)
    index_name, backup_index_name = swap_backup_index(
        es, logger, alias, index_name, backup_index_name
    )
    logger.info("Deleting and recreating %s" % index_name)
    es.indices.delete(index=index_name)
    logger.debug("Creating %s with mappings and settings" % index_name)
    es.indices.create(
        index=index_name,
        body={
            "settings": settings,
            "mappings": {doc_type_name: mapping}
        }
    )
    logger.info(
        "Loading data into %s with doc_type %s"
        % (index_name, doc_type_name)
    )
    try:
        total_rows_of_data = 0
        for doc_ary in yield_chunked_docs(
            data_load_strategy_complaint, data, chunk_size
        ):
            logger.info("chunk retrieved, now bulk load")
            success, _ = bulk(
                es, actions=doc_ary, index=index_name,
                doc_type=doc_type_name, chunk_size=chunk_size, refresh=True
            )
            total_rows_of_data += success
            logger.info(
                "%d records indexed, total = %d"
                % (success, total_rows_of_data)
            )
        update_indexes_in_alias(
            es, logger, alias, backup_index_name, index_name
        )
    except TransportError as e:
        logger.error(
            "Error while loading data. Reverting alias to original "
            "state after: %s" % e
        )
        es.indices.put_alias(name=alias, index=backup_index_name)
        sys.exit(e.error)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def build_arg_parser():
    p = configargparse.getArgumentParser(
        prog='index_ccdb',
        description='fill Elasticsearch with public complaint data',
        ignore_unknown_config_file_keys=True,
        default_config_files=['./config.ini'],
        args_for_setting_config_path=['-c', '--config'],
        args_for_writing_out_config_file=['--save-config']
    )
    p.add('--dump-config', action='store_true', dest='dump_config',
          help='dump config vars and their source')
    group = add_basic_es_arguments(p)
    group.add('--settings', required=True,
              help="Describes how the ES index should function")
    group.add('--mapping', required=True,
              help="Describes how process the input documents")
    group.add('--doc-type', dest='doc_type', default='complaint',
              help='Elasticsearch document type')
    group = p.add_argument_group('Files')
    group.add('--dataset', dest='dataset', required=True,
              help="Complaint data in NDJSON format")
    return p


if __name__ == '__main__':
    p = build_arg_parser()
    cfg = p.parse_args()

    logger = setup_logging(cfg.doc_type)

    if cfg.dump_config:
        logger.info('Running index_ccdb with')
        logger.info(p.format_values())

    index_alias = cfg.index_name
    index_name = "{}-v1".format(index_alias)
    backup_index_name = "{}-v2".format(index_alias)

    logger.info("Creating Elasticsearch Connection")
    es = get_es_connection(cfg)

    logger.info("Begin indexing data in Elasticsearch")
    index_json_data(
        es, logger, cfg.doc_type,
        cfg.settings,
        cfg.mapping,
        cfg.dataset,
        index_name, backup_index_name, index_alias
    )
