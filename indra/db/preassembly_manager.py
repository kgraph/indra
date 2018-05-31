import json
from collections import defaultdict

import logging
from datetime import datetime
from functools import wraps


logger = logging.getLogger('db_preassembly')


if __name__ == '__main__':
    # NOTE: PEP8 will complain about this, however having the args parsed up
    # here prevents a long wait just to fined out you entered a command wrong.
    from argparse import ArgumentParser
    parser = ArgumentParser(
        description='Manage content on INDRA\'s database.'
        )
    parser.add_argument(
        choices=['create', 'update'],
        dest='task',
        help=('Choose whether you want to perform an initial upload or update '
              'the existing content on the database.')
        )
    parser.add_argument(
        '-c', '--continue',
        dest='continuing',
        action='store_true',
        help='Continue uploading or updating, picking up where you left off.'
        )
    parser.add_argument(
        '-n', '--num_procs',
        dest='num_procs',
        type=int,
        default=1,
        help=('Select the number of processors to use during this operation. '
              'Default is 1.')
        )
    parser.add_argument(
        '-b', '--batch',
        type=int,
        default=10000,
        help=("Select the number of statements loaded at a time. More "
              "statements at a time will run faster, but require more memory.")
    )
    parser.add_argument(
        '-d', '--debug',
        dest='debug',
        action='store_true',
        help='Run with debugging level output.'
        )
    parser.add_argument(
        '-D', '--database',
        default='primary',
        help=('Select a database from the names given in the config or '
              'environment, for example primary is INDRA_DB_PRIMAY in the '
              'config file and INDRADBPRIMARY in the environment. The default '
              'is \'primary\'. Note that this is overwridden by use of the '
              '--test flag if \'test\' is not a part of the name given.')
        )
    args = parser.parse_args()
    if args.debug:
        logger.setLevel(logging.DEBUG)
        from indra.db.database_manager import logger as db_logger
        db_logger.setLevel(logging.DEBUG)


import indra.tools.assemble_corpus as ac
from indra.preassembler import Preassembler
from indra.preassembler.hierarchy_manager import hierarchies
from indra.db.util import insert_pa_stmts, distill_stmts, get_db
from indra.statements import Statement
from indra.util import batch_iter


def _handle_update_table(func):
    @wraps(func)
    def run_and_record_update(cls, db, *args, **kwargs):
        run_datetime = datetime.utcnow()
        completed = func(cls, db, *args, **kwargs)
        if completed:
            is_corpus_init = (func.__name__ == 'create_corpus')
            db.insert('preassembly_updates', corpus_init=is_corpus_init,
                      run_datetime=run_datetime)
        return completed
    return run_and_record_update


def _stmt_from_json(stmt_json_bytes):
    return Statement._from_json(json.loads(stmt_json_bytes.decode('utf-8')))


class IndraDBPreassemblyError(Exception):
    pass


class PreassemblyManager(object):
    """Class used to manage the preassembly pipeline

    Parameters
    ----------
    n_proc : int
        Select the number of processes that will be used when performing
        preassembly. Default is 1.
    batch_size : int
        Select the maximum number of statements you wish to be handled at a
        time. In general, a larger batch size will somewhat be faster, but
        require much more memory.
    """
    def __init__(self, n_proc=1, batch_size=10000):
        self.n_proc = n_proc
        self.batch_size = batch_size
        self.pa = Preassembler(hierarchies)
        return

    def _get_latest_updatetime(self, db):
        """Get the date of the latest update."""
        update_list = db.select_all(db.PreassemblyUpdates)
        if not len(update_list):
            logger.warning("The preassembled corpus has not been initialized, "
                           "or else the updates table has not been populated.")
            return None
        return max([u.run_datetime for u in update_list])

    def _pa_batch_iter(self, db, mk_set=None):
        """Return an iterator over batches of preassembled statements.

        This avoids the need to load all such statements from the database into
        RAM at the same time (as this can be quite large).

        You may limit the set of pa_statements loaded by providing a set/list of
        matches-keys of the statements you wish to include.
        """
        if mk_set is None:
            db_stmt_iter = db.select_all(db.PAStatements.json,
                                         yield_per=self.batch_size)
        else:
            db_stmt_iter = db.select_all(db.PAStatements.json,
                                         db.PAStatements.mk_hash.in_(mk_set),
                                         yield_per=self.batch_size)
        pa_stmts = (_stmt_from_json(s_json) for s_json, in db_stmt_iter)
        return batch_iter(pa_stmts, self.batch_size, return_lists=True)

    def _get_unique_statements(self, db, stmts, num_stmts, mk_done=None):
        """Get the unique Statements from the raw statements."""
        if mk_done is None:
            mk_done = set()

        new_mk_set = set()
        for stmt_batch in batch_iter(stmts, self.batch_size, return_lists=True):
            logger.info("Processing batch of %d/%d statements."
                        % (len(stmt_batch), num_stmts))
            unique_stmts, evidence_links = \
                self._make_unique_statement_set(stmt_batch)
            new_unique_stmts = []
            for s in unique_stmts:
                s_hash = s.get_hash(shallow=True)
                if s_hash not in (mk_done | new_mk_set):
                    new_mk_set.add(s_hash)
                    new_unique_stmts.append(s)
            insert_pa_stmts(db, new_unique_stmts)
            db.copy('raw_unique_links', evidence_links,
                    ('pa_stmt_mk_hash', 'raw_stmt_uuid'))
        return new_mk_set

    @_handle_update_table
    def create_corpus(self, db, continuing=False):
        """Initialize the table of preassembled statements.

        This method will find the set of unique knowledge represented in the
        table of raw statements, and it will populate the table of preassembled
        statements (PAStatements/pa_statements), while maintaining links between
        the raw statements and their unique (pa) counterparts. Furthermore, the
        refinement/support relationships between unique statements will be found
        and recorded in the PASupportLinks/pa_support_links table.

        For more detail on preassembly, see indra/preassembler/__init__.py
        """
        # Get the statements
        stmt_ids = distill_stmts(db)
        if continuing:
            checked_raw_stmt_ids, pa_stmt_hashes = \
                zip(*db.select_all([db.RawUniqueLinks.raw_stmt_uuid,
                                    db.RawUniqueLinks.pa_stmt_mk_hash]))
            stmt_ids -= set(checked_raw_stmt_ids)
            done_pa_ids = set(pa_stmt_hashes)
        else:
            done_pa_ids = set()
        stmts = (_stmt_from_json(s_json) for s_json,
                 in db.select_all(db.RawStatements.json,
                                  db.RawStatements.uuid.in_(stmt_ids),
                                  yield_per=self.batch_size))
        logger.info("Found %d statements in all." % len(stmt_ids))

        # Get the set of unique statements
        if stmt_ids:
            self._get_unique_statements(db, stmts, len(stmt_ids), done_pa_ids)

        # Now get the support links between all batches.
        support_links = set()
        for i, outer_batch in enumerate(self._pa_batch_iter(db)):
            for j, inner_batch in enumerate(self._pa_batch_iter(db)):
                if i != j:
                    split_idx = len(inner_batch)
                    full_list = inner_batch + outer_batch
                    support_links |= \
                        self._get_support_links(full_list, split_idx=split_idx,
                                                poolsize=self.n_proc)
                else:
                    support_links |= \
                        self._get_support_links(inner_batch,
                                                poolsize=self.n_proc)
        db.copy('pa_support_links', support_links,
                ('supported_mk_hash', 'supporting_mk_hash'))
        return True

    @_handle_update_table
    def supplement_corpus(self, db):
        """Update the table of preassembled statements.

        This method will take any new raw statements that have not yet been
        incorporated into the preassembled table, and use them to augment the
        preassembled table.

        The resulting updated table is indistinguishable from the result you
        would achieve if you had simply re-run preassembly on _all_ the
        raw statements.
        """
        last_update = self._get_latest_updatetime(db)
        logger.info("Latest update was: %s" % last_update)

        # Get the new statements...
        old_uuid_q = db.filter_query(
            db.RawStatements.uuid,
            db.RawStatements.uuid == db.RawUniqueLinks.raw_stmt_uuid
        )
        new_uuid_q = db.filter_query(db.RawStatements.uuid).except_(old_uuid_q)
        all_new_stmt_ids = {uuid for uuid, in new_uuid_q.all()}

        stmt_ids = distill_stmts(db)
        new_stmt_ids = all_new_stmt_ids & stmt_ids
        new_stmts = (_stmt_from_json(s_json) for s_json,
                     in db.select_all(db.RawStatements.json,
                                      db.RawStatements.uuid.in_(new_stmt_ids),
                                      yield_per=self.batch_size))
        logger.info("Found %d new statements." % len(new_stmt_ids))

        # Get the set of new unique statements.
        old_mk_set = {mk for mk, in db.select_all(db.PAStatements.mk_hash)}
        new_mk_set = self._get_unique_statements(db, new_stmts,
                                                 len(new_stmt_ids), old_mk_set)

        # Now find the new support links that need to be added.
        new_support_links = set()
        for npa_batch in self._pa_batch_iter(db, mk_set=new_mk_set):
            # Compare internally
            new_support_links |= self._get_support_links(npa_batch)

            # Compare against the other new batch statements.
            diff_new_mks = new_mk_set - {s.get_hash(shallow=True)
                                         for s in npa_batch}
            for diff_npa_batch in self._pa_batch_iter(db, mk_set=diff_new_mks):
                split_idx = len(npa_batch)
                full_list = npa_batch + diff_npa_batch
                new_support_links |= \
                    self._get_support_links(full_list, split_idx=split_idx,
                                            poolsize=self.n_proc)

            # Compare against the existing statements.
            for opa_batch in self._pa_batch_iter(db, mk_set=old_mk_set):
                split_idx = len(npa_batch)
                full_list = npa_batch + opa_batch
                new_support_links |= \
                    self._get_support_links(full_list, split_idx=split_idx,
                                            poolsize=self.n_proc)

        db.copy('pa_support_links', new_support_links,
                ('supported_mk_hash', 'supporting_mk_hash'))
        return True

    def _make_unique_statement_set(self, stmts):
        """Perform grounding, sequence mapping, and find unique set from stmts.

        This method returns a list of statement objects, as well as a set of
        tuples of the form (uuid, matches_key) which represent the links between
        raw (evidence) statements and their unique/preassembled counterparts.
        """
        stmts = ac.map_grounding(stmts)
        stmts = ac.map_sequence(stmts)
        stmt_groups = self.pa._get_stmt_matching_groups(stmts)
        unique_stmts = []
        evidence_links = defaultdict(lambda: set())
        for _, duplicates in stmt_groups:
            # Get the first statement and add the evidence of all subsequent
            # Statements to it
            for stmt_ix, stmt in enumerate(duplicates):
                if stmt_ix == 0:
                    first_stmt = stmt.make_generic_copy()
                evidence_links[first_stmt.get_hash(shallow=True)].add(stmt.uuid)
            # This should never be None or anything else
            assert isinstance(first_stmt, type(stmt))
            unique_stmts.append(first_stmt)
        return unique_stmts, flatten_evidence_dict(evidence_links)

    def _process_statements(self, stmts, **generate_id_map_kwargs):
        """Perform the full process pipeline. (Currently only used in tests)"""
        # TODO: This should probably be somehow placed in canonical preassembly.
        unique_stmts, evidence_links = self._make_unique_statement_set(stmts)
        match_key_maps = self._get_support_links(unique_stmts,
                                                 **generate_id_map_kwargs)
        unique_stmt_dict = {stmt.get_hash(shallow=True): stmt
                            for stmt in unique_stmts}
        return unique_stmt_dict, evidence_links, match_key_maps

    def _get_support_links(self, unique_stmts, **generate_id_map_kwargs):
        """Find the links of refinement/support between statements."""
        id_maps = self.pa._generate_id_maps(unique_stmts,
                                            **generate_id_map_kwargs)
        return {tuple([unique_stmts[idx].get_hash(shallow=True)
                       for idx in idx_pair])
                for idx_pair in id_maps}

    def _get_increment_links(self, unique_stmt_dict, new_unique_stmt_dict,
                             **kwargs):
        """Perform the update process. (Currently only used in tests)"""
        new_support_links = set()
        # Now get the list of statements the need to be compared between the
        # existing and new corpora
        old_stmt_hash_set = set(unique_stmt_dict.keys())
        new_stmt_hash_set = set(new_unique_stmt_dict.keys())
        only_old_stmts = [unique_stmt_dict[mk_hash]
                          for mk_hash in old_stmt_hash_set - new_stmt_hash_set]
        only_new_stmts = [new_unique_stmt_dict[mk_hash]
                          for mk_hash in new_stmt_hash_set - old_stmt_hash_set]
        logger.info("There were %d exclusively old statements and we are "
                    "adding %d exclusively new statements. There were %d "
                    "overlapping statements."
                    % (len(only_old_stmts), len(only_new_stmts),
                       len(new_stmt_hash_set & old_stmt_hash_set)))
        split_idx = len(only_old_stmts) + 1
        merge_stmts = only_old_stmts + only_new_stmts

        # Find the support links between the new corpora
        merge_support_links = self._get_support_links(merge_stmts,
                                                      split_idx=split_idx,
                                                      **kwargs)
        logger.info("Found %d links between old and new corpora."
                    % len(merge_support_links))

        new_support_links |= merge_support_links
        return new_support_links


def make_graph(unique_stmts, match_key_maps):
    """Create a networkx graph of the statement and their links."""
    import networkx as nx
    g = nx.Graph()
    link_matches = {m for l in match_key_maps for m in l}
    unique_stmts_dict = {}
    for stmt in unique_stmts:
        if stmt.matches_key() in link_matches:
            g.add_node(stmt)
            unique_stmts_dict[stmt.matches_key()] = stmt

    for k1, k2 in match_key_maps:
        g.add_edge(unique_stmts_dict[k1], unique_stmts_dict[k2])

    return g


def flatten_evidence_dict(ev_dict):
    return {(u_stmt_key, ev_stmt_uuid)
            for u_stmt_key, ev_stmt_uuid_set in ev_dict.items()
            for ev_stmt_uuid in ev_stmt_uuid_set}


if __name__ == '__main__':
    print("Getting %s database." % args.database)
    db = get_db(args.database)
    assert db is not None
    db.grab_session()
    pm = PreassemblyManager(args.num_procs, args.batch)

    print("Beginning to %s preassembled corpus." % args.task)
    if args.task == 'create':
        pm.create_corpus(db, args.continuing)
    elif args.task == 'update':
        pm.supplement_corpus(db)
    else:
        raise IndraDBPreassemblyError('Unrecognized task: %s.' % args.task)
