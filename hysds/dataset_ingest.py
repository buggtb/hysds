from __future__ import absolute_import

import os, sys, re, traceback, json, requests, shutil, types, socket, backoff
import math
from subprocess import check_output, check_call
from fabric.api import env, get, run, put
from fabric.contrib.files import exists
from pprint import pprint, pformat
from urlparse import urlparse
from lxml.etree import parse
from StringIO import StringIO
from glob import glob
from datetime import datetime
from filechunkio import FileChunkIO
import hysds, osaka.main
from hysds.utils import get_disk_usage, makedirs
from hysds.log_utils import (logger, log_publish_prov_es, backoff_max_value,
backoff_max_tries)
from hysds.recognize import Recognizer


FILE_RE = re.compile(r'file://(.*?)(/.*)$')
SCRIPT_RE = re.compile(r'script:(.*)$')
BROWSE_RE = re.compile(r'^(.+)\.browse\.png$')


def verify_dataset(dataset):
    """Verify dataset JSON fields."""

    if 'version' not in dataset:
        raise RuntimeError("Failed to find required field: version")
    for field in ('label', 'location', 'starttime', 'endtime', 'creation_timestamp'):
        if field not in dataset:
            logger.info("Optional field not found: %s" % field)


@backoff.on_exception(backoff.expo,
                      requests.RequestException,
                      max_tries=backoff_max_tries,
                      max_value=backoff_max_value)
def index_dataset(grq_update_url, update_json):
    """Index dataset into GRQ ES."""

    r = requests.post(grq_update_url, verify=False,
                      data={ 'dataset_info': json.dumps(update_json)})
    r.raise_for_status()
    return r.json()


def queue_dataset(dataset, update_json, queue_name):
    """Add dataset type and URL to queue."""

    payload = {
        'job_type': 'dataset:%s' % dataset,
        'payload': update_json
    }
    hysds.orchestrator.do_submit_job(payload, queue_name)


def get_remote_dav(url):
    """Get remote dir/file."""

    lpath = './%s' % os.path.basename(url)
    if not url.endswith('/'): url += '/'
    parsed_url = urlparse(url)
    rpath = parsed_url.path
    r = requests.request('PROPFIND', url, verify=False)
    if r.status_code not in (200, 207): # handle multistatus (207) as well
        logger.info("Got status code %d trying to read %s" % (r.status_code, url))
        logger.info("Content:\n%s" % r.text)
        r.raise_for_status()
    tree = parse(StringIO(r.content))
    makedirs(lpath)
    for elem in tree.findall('{DAV:}response'):
        collection = elem.find('{DAV:}propstat/{DAV:}prop/{DAV:}resourcetype/{DAV:}collection')
        if collection is not None: continue
        href = elem.find('{DAV:}href').text
        rel_path = os.path.relpath(href, rpath)
        file_url = os.path.join(url, rel_path)
        local_path = os.path.join(lpath, rel_path)
        local_dir = os.path.dirname(local_path)
        makedirs(local_dir)
        resp = requests.request('GET', file_url, verify=False, stream=True)
        if resp.status_code != 200:
            logger.info("Got status code %d trying to read %s" % (resp.status_code, file_url))
            logger.info("Content:\n%s" % resp.text)
        resp.raise_for_status()
        with open(local_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=1024):
                if chunk: # filter out keep-alive new chunks
                    f.write(chunk)
                    f.flush()
    return os.path.abspath(lpath)


def get_remote(host, rpath):
    """Get remote dir/file."""

    env.host_string = host
    env.abort_on_prompts = True
    r = get(rpath, '.')
    return os.path.abspath('./%s' % os.path.basename(rpath))


def move_remote_path(host, src, dest):
    """Move remote directory safely."""

    env.host_string = host
    env.abort_on_prompts = True
    dest_dir = os.path.dirname(dest)
    if not exists(dest_dir):
        run("mkdir -p %s" % dest_dir)
    ret = run("mv -f %s %s" % (src, dest)) 
    return ret 



def restage(host, src, dest, signal_file):
    """Restage dataset and create signal file."""

    env.host_string = host
    env.abort_on_prompts = True
    dest_dir = os.path.dirname(dest)
    if not exists(dest_dir):
        run("mkdir -p %s" % dest_dir)
    run("mv -f %s %s" % (src, dest)) 
    ret = run("touch %s" % signal_file)
    return ret 


def publish_dataset(path, url, params=None, force=False):
    '''
    Publish a dataset to the given url
    @param path - path of dataset to publish
    @param url - url to publish to
    '''

    # set osaka params
    if params is None: params = {}

    # force remove previous dataset if it exists?
    if force:
        try: unpublish_dataset(url, params=params)
        except: pass

    # upload datasets 
    for root, dirs, files in os.walk(path):
        for file in files:
            abs_path = os.path.join(root, file)
            rel_path = os.path.relpath(abs_path, path)
            dest_url = os.path.join(url, rel_path)
            logger.info("Uploading %s to %s." % (abs_path, dest_url))
            osaka.main.put(abs_path, dest_url, params=params, noclobber=True)


def unpublish_dataset(url, params=None):
    '''
    Remove a dataset at (and below) the given url
    @param url - url to remove files (at and below)
    '''

    # set osaka params
    if params is None: params = {}

    osaka.main.rmall(url, params=params)


def ingest(objectid, dsets_file, grq_update_url, dataset_processed_queue,
           prod_path, job_path, dry_run=False, force=False):
    """Run dataset ingest."""
    logger.info("#" * 80)
    logger.info("datasets: %s" % dsets_file)
    logger.info("grq_update_url: %s" % grq_update_url)
    logger.info("dataset_processed_queue: %s" % dataset_processed_queue)
    logger.info("prod_path: %s" % prod_path)
    logger.info("job_path: %s" % job_path)
    logger.info("dry_run: %s" % dry_run)
    logger.info("force: %s" % force)

    # get dataset
    if os.path.isdir(prod_path):
        local_prod_path = prod_path
    else:
        local_prod_path = get_remote_dav(prod_path)
    if not os.path.isdir(local_prod_path):
        raise RuntimeError("Failed to find local dataset directory: %s" % local_prod_path)

    # dataset name
    pname = os.path.basename(local_prod_path)

    # dataset file
    dataset_file = os.path.join(local_prod_path, '%s.dataset.json' % pname)

    # get dataset json
    with open(dataset_file) as f:
        dataset = json.load(f)
    logger.info("Loaded dataset JSON from file: %s" % dataset_file)

    # check minimum requirements for dataset JSON
    logger.info("Verifying dataset JSON...")
    verify_dataset(dataset)
    logger.info("Dataset JSON verfication succeeded.")

    # get version
    version = dataset['version']

    # recognize
    r = Recognizer(dsets_file, local_prod_path, objectid, version)

    # get ipath
    ipath = r.currentIpath

    # get extractor
    extractor = r.getMetadataExtractor()
    if extractor is not None:
        match = SCRIPT_RE.search(extractor)
        if match: extractor = match.group(1)
    logger.info("Configured metadata extractor: %s" % extractor)

    # metadata file
    metadata_file = os.path.join(local_prod_path, '%s.met.json' % pname)

    # metadata seed file
    seed_file = os.path.join(local_prod_path, 'met.json')

    # metadata file already here
    if os.path.exists(metadata_file):
        with open(metadata_file) as f:
            metadata = json.load(f)
        logger.info("Loaded metadata from existing file: %s" % metadata_file)
    else:
        if extractor is None:
            logger.info("No metadata extraction configured. Setting empty metadata.")
            metadata = {}
        else:
            logger.info("Running metadata extractor %s on %s" % (extractor, local_prod_path))
            m = check_output([extractor, local_prod_path])
            logger.info("Output: %s" % m)

            # generate json to update metadata and urls
            metadata = json.loads(m)

            # set data_product_name
            metadata['data_product_name'] = objectid

            # merge with seed metadata
            if os.path.exists(seed_file):
                with open(seed_file) as f:
                    seed = json.load(f)
                metadata.update(seed)
                logger.info("Loaded seed metadata from file: %s" % seed_file)

            # write it out to file
            with open(metadata_file, 'w') as f:
                json.dump(metadata, f, indent=2)
            logger.info("Wrote metadata to %s" % metadata_file)

            # delete seed file
            if os.path.exists(seed_file):
                os.unlink(seed_file)
                logger.info("Deleted seed file %s." % seed_file)

    # add context
    context_file = os.path.join(local_prod_path, '%s.context.json' % pname)
    if os.path.exists(context_file):
        with open(context_file) as f:
            context = json.load(f)
        logger.info("Loaded context from existing file: %s" % context_file)
    else: context = {}
    metadata['context'] = context

    # set metadata and dataset groups in recognizer
    r.setDataset(dataset)
    r.setMetadata(metadata)

    # get level
    level = r.getLevel()

    # get type
    dtype = r.getType()

    # get publish path
    pub_path_url = r.getPublishPath()

    # get publish urls
    pub_urls = [i for i in r.getPublishUrls()]

    # get S3 profile name and api keys for dataset publishing
    s3_secret_key, s3_access_key = r.getS3Keys()
    s3_profile = r.getS3Profile()

    # set osaka params
    osaka_params = {}

    # S3 profile takes precedence over explicit api keys
    if s3_profile is not None:
        osaka_params['profile_name'] = s3_profile
    else:
        if s3_secret_key is not None and s3_access_key is not None:
            osaka_params['aws_access_key_id'] = s3_access_key
            osaka_params['aws_secret_access_key'] = s3_secret_key

    # get browse path and urls
    browse_path = r.getBrowsePath()
    browse_urls = r.getBrowseUrls()

    # get S3 profile name and api keys for browse image publishing
    s3_secret_key_browse, s3_access_key_browse = r.getS3Keys("browse")
    s3_profile_browse = r.getS3Profile("browse")

    # set osaka params for browse
    osaka_params_browse = {}

    # S3 profile takes precedence over explicit api keys
    if s3_profile_browse is not None:
        osaka_params_browse['profile_name'] = s3_profile_browse
    else:
        if s3_secret_key_browse is not None and s3_access_key_browse is not None:
            osaka_params_browse['aws_access_key_id'] = s3_access_key_browse
            osaka_params_browse['aws_secret_access_key'] = s3_secret_key_browse

    # get pub host and path
    logger.info("Configured pub host & path: %s" % (pub_path_url))

    # check scheme
    if not osaka.main.supported(pub_path_url):
        raise RuntimeError("Scheme %s is currently not supported." % urlparse(pub_path_url).scheme)

    # upload dataset to repo; track disk usage and start/end times of transfer
    prod_dir_usage = get_disk_usage(local_prod_path)
    tx_t1 = datetime.utcnow()
    if dry_run:
        logger.info("Would've published %s to %s" % (local_prod_path, pub_path_url))
    else:
        publish_dataset(local_prod_path, pub_path_url, params=osaka_params, force=force)
    tx_t2 = datetime.utcnow()

    # add metadata for all browse images and upload to browse location
    imgs_metadata = []
    imgs = glob('%s/*browse.png' % local_prod_path)
    for img in imgs:
        img_metadata = { 'img': os.path.basename(img) }
        small_img = img.replace('browse.png', 'browse_small.png')
        if os.path.exists(small_img):
            small_img_basename = os.path.basename(small_img)
            if browse_path is not None:
                this_browse_path = os.path.join(browse_path, small_img_basename)
                if dry_run:
                    logger.info("Would've uploaded %s to %s" % (small_img, browse_path))
                else:
                    logger.info("Uploading %s to %s" % (small_img, browse_path))
                    osaka.main.put(small_img, this_browse_path,
                                   params=osaka_params_browse, noclobber=False)
        else: small_img_basename = None
        img_metadata['small_img'] = small_img_basename
        tooltip_match = BROWSE_RE.search(img_metadata['img'])
        if tooltip_match: img_metadata['tooltip'] = tooltip_match.group(1)
        else: img_metadata['tooltip'] = ""
        imgs_metadata.append(img_metadata)

    # sort browse images
    browse_sort_order = r.getBrowseSortOrder()
    if isinstance(browse_sort_order, types.ListType) and len(browse_sort_order) > 0:
        bso_regexes = [re.compile(i) for i in browse_sort_order]
        sorter =  {}
        unrecognized = []
        for img in imgs_metadata:
            matched = None
            for i, bso_re in enumerate(bso_regexes):
                if bso_re.search(img['img']):
                    matched = img
                    sorter[i] = matched
                    break
            if matched is None: unrecognized.append(img)
        imgs_metadata = [sorter[i] for i in sorted(sorter)]
        imgs_metadata.extend(unrecognized)

    # save dataset metrics on size and transfer
    tx_dur = (tx_t2 - tx_t1).total_seconds()
    prod_metrics = {
        'ipath': ipath,
        'url': urlparse(pub_path_url).path,
        'path': local_prod_path,
        'disk_usage': prod_dir_usage,
        'time_start': tx_t1.isoformat() + 'Z',
        'time_end': tx_t2.isoformat() + 'Z',
        'duration': tx_dur,
        'transfer_rate': prod_dir_usage/tx_dur
    }

    # set update json
    ipath = r.currentIpath
    update_json = {
        'id': objectid,
        'objectid': objectid,
        'metadata': metadata,
        'urls': pub_urls,
        'browse_urls': browse_urls,
        'images': imgs_metadata,
        'dataset': ipath.split('/')[1],
        'ipath': ipath,
        'system_version': version,
        'dataset_level': level,
        'dataset_type': dtype,
    }
    update_json.update(dataset)
    #logger.info("update_json: %s" % pformat(update_json))

    # update GRQ
    if isinstance(update_json['metadata'], types.DictType) and len(update_json['metadata']) > 0:
        #logger.info("update_json: %s" % pformat(update_json))
        if dry_run:
            logger.info("Would've indexed doc at %s: %s" % (grq_update_url, 
                                                            json.dumps(update_json, indent=2, sort_keys=True)))
        else:
            res = index_dataset(grq_update_url, update_json)
            logger.info("res: %s" % res)

    # finish if dry run
    if dry_run: return (prod_metrics, update_json)

    # create PROV-ES JSON file for publish processStep
    prod_prov_es_file = os.path.join(local_prod_path, '%s.prov_es.json' % os.path.basename(local_prod_path))
    pub_prov_es_bn = "publish.prov_es.json"
    if os.path.exists(prod_prov_es_file):
        pub_prov_es_file = os.path.join(local_prod_path, pub_prov_es_bn)
        prov_es_info = {}
        with open(prod_prov_es_file) as f:
            try: prov_es_info = json.load(f)
            except Exception, e:
                tb = traceback.format_exc()
                raise(RuntimeError("Failed to load PROV-ES from %s: %s\n%s" % (prod_prov_es_file, str(e), tb)))
        log_publish_prov_es(prov_es_info, pub_prov_es_file, local_prod_path,
                            pub_urls, prod_metrics, objectid)
        # upload publish PROV-ES file
        osaka.main.put(pub_prov_es_file, os.path.join(pub_path_url, pub_prov_es_bn),
                       params=osaka_params, noclobber=False)
    
    # queue data dataset
    queue_dataset(ipath, update_json, dataset_processed_queue)

    # return dataset metrics and dataset json
    return (prod_metrics, update_json)
