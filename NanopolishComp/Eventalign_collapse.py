# -*- coding: utf-8 -*-

#~~~~~~~~~~~~~~IMPORTS~~~~~~~~~~~~~~#
# Standard library imports
import multiprocessing as mp
from time import time
from collections import OrderedDict

# Third party imports
import numpy as np
from tqdm import tqdm

# Local imports
from NanopolishComp.Helper_fun import stderr_print

#~~~~~~~~~~~~~~CLASS~~~~~~~~~~~~~~#
class Eventalign_collapse ():
    """
    Collapse the nanopolish eventalign output by kmers rather that by events.
    kmer level statistics (mean, median, std, var) are only computed if nanopolish is run with --samples option
    """

    def __init__ (self, output_fn, input_fn=0, threads=4, max_reads=None, write_samples=False, verbose=False):
        """
        * output_fn
            Path the output eventalign collapsed tsv file
        * input_fn
            Path to a nanopolish eventalign tsv output file. If '0' read from std input (default = 0)
        * threads
            Total number of threads. 1 thread is used for the reader and 1 for the writer (default = 4)
        * max_reads
            Maximum number of read to parse. 0 to deactivate (default = 0)
        * write_samples
            If given, will write the raw sample if eventalign is run with --samples option
        * verbose
            .....
        """
        self.output_fn = output_fn
        self.input_fn = input_fn
        self.threads = threads-2 # Remove 2 threads for read and write
        self.max_reads = max_reads
        self.write_samples = write_samples
        self.verbose = verbose

        if self.verbose: stderr_print ("Collapse file by read_id/ref_id\n")
        # Init Multiprocessing variables
        header_q = mp.Queue ()
        in_q = mp.Queue (maxsize = 1000)
        out_q = mp.Queue (maxsize = 1000)

        # Define processes
        ps_list = []
        ps_list.append (mp.Process (target=self._split_reads, args=(header_q, in_q,)))
        for i in range (self.threads):
            ps_list.append (mp.Process (target=self._process_read, args=(header_q, in_q, out_q)))
        ps_list.append (mp.Process (target=self._write_output, args=(out_q,)))

        # Start processes and block until done
        try:
            for ps in ps_list:
                ps.start ()
            for ps in ps_list:
                ps.join ()

        # Kill processes if early stop
        except (BrokenPipeError, KeyboardInterrupt) as E:
            if self.verbose: stderr_print ("Early stop. Kill processes\n")
            for ps in ps_list:
                ps.terminate ()

    #~~~~~~~~~~~~~~PRIVATE METHODS~~~~~~~~~~~~~~#
    def _split_reads (self, header_q, in_q):
        """
        Mono-threaded reader
        """
        # Open input file or stdin if 0
        with open (self.input_fn) as fp:

            # Get header line and pass it to worker processes through header_q
            input_header = fp.readline().rstrip().split("\t")

            for i in range (self.threads):
                header_q.put (input_header)

            # Get read id and ref id indexes
            ref_id_idx = input_header.index ("contig")
            if "read_name" in input_header:
                read_id_idx = input_header.index ("read_name")
            elif "read_index" in input_header:
                read_id_idx = input_header.index ("read_index")

            # First data line
            ls = fp.readline().rstrip().split("\t")
            read_id=ls[read_id_idx]
            ref_id=ls[ref_id_idx]
            event_list = [ls]
            n_reads = 1

            for line in fp:

                # Early ending if required
                if self.max_reads and n_reads == self.max_reads:
                    break

                ls = line.rstrip().split("\t")

                # Line correspond to the same ids
                if ls[read_id_idx] == read_id and ls[ref_id_idx] == ref_id:
                    event_list.append(ls)

                # New ids = enqueue prev list and start new one
                else:
                    # Put read_list in queue and update counter
                    in_q.put ((read_id, ref_id, event_list))
                    n_reads+=1

                    # Reset values
                    read_id=ls[read_id_idx]
                    ref_id=ls[ref_id_idx]
                    event_list = [ls]

            # Last data line
            if event_list:
                in_q.put ((read_id, ref_id, event_list))

        # Add 1 poison pill for each worker thread
        for i in range (self.threads):
            in_q.put(None)

    def _process_read (self, header_q, in_q, out_q):
        """
        Multi-threaded workers
        """

        # Get the header from the header q (one per thread)
        input_header = header_q.get()

        # Get index of the fields we are interested in
        idx = self._get_field_idx (input_header)

        # Prepare output header based on fields in the input_header
        output_header = self._make_ouput_header (input_header)

        # Collapse event at kmer level
        for read_id, ref_id, event_list in iter(in_q.get, None):

            # Write read header to str
            read_str = "#{}\t{}\n".format (read_id, ref_id)
            read_str+= "{}\n".format (output_header)

            # Init values for first kmer
            kmer_d = self._init_kmer_dict (event_list[0], idx)

            # init read dictionary
            read_d = OrderedDict ()
            read_d["read_id"] = read_id
            read_d["ref_id"] = ref_id
            read_d["kmers"] = 0
            read_d["NNNNN_kmers"] = 0
            read_d["mismatching_kmers"] = 0
            read_d["missing_kmers"] = 0
            read_d["ref_start"] = kmer_d["pos"]

            # Iterate over the rest of the lines
            for event in event_list [1:]:
                pos_dif = abs (int(event[idx["pos"]])-int(kmer_d["pos"]))

                # Same position = update current kmer
                if pos_dif == 0:
                    kmer_d = self._update_kmer_dict (kmer_d, event, idx)

                # New position = write previous kmer and start new one
                else:
                    # Update read counter
                    read_d["kmers"] += 1
                    if kmer_d ["NNNNN_events"] >= 1: read_d["NNNNN_kmers"] += 1
                    if kmer_d ["mismatching_events"] >= 1: read_d["mismatching_kmers"] += 1
                    if pos_dif >=2: read_d["missing_kmers"] += (pos_dif-1)

                    # Converts previous kmer to str and init new kmer
                    read_str += self._kmer_dict_to_str (kmer_d, idx)
                    kmer_d = self._init_kmer_dict (event, idx)

            # Last read_d update
            read_d["kmers"] += 1
            if kmer_d ["NNNNN_events"] >= 1: read_d["NNNNN_kmers"] += 1
            if kmer_d ["mismatching_events"] >= 1: read_d["mismatching_kmers"] += 1
            if pos_dif >=2: read_d["missing_kmers"] += (pos_dif-1)
            read_d["ref_end"] = int(kmer_d["pos"])+1 ### Off by 1 error if using python indexing

            # Last kmer
            read_str += self._kmer_dict_to_str (kmer_d, idx)

            # Add the current read details to queue
            out_q.put ((read_d, read_str))

        # Add poison pill in queues
        out_q.put (None)

    def _write_output (self, out_q):
        """
        Mono-threaded Writer
        """
        offset = n_reads = 0
        t = time()

        # Open output files
        with open (self.output_fn, "w") as output_fp,\
             open (self.output_fn+".idx", "w") as idx_fp,\
             tqdm (unit=" reads", mininterval=0.1, smoothing=0.1, disable= not self.verbose) as pbar:

            idx_fp.write ("ref_id\tref_start\tref_end\tread_id\tkmers\tNNNNN_kmers\tmismatching_kmers\tmissing_kmers\toffset\n")

            n_reads = 0
            for _ in range (self.threads):
                for (read_d, read_str) in iter (out_q.get, None):

                    output_fp.write (read_str)
                    idx_fp.write ("{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\n".format (
                        read_d["ref_id"],
                        read_d["ref_start"],
                        read_d["ref_end"],
                        read_d["read_id"],
                        read_d["kmers"],
                        read_d["NNNNN_kmers"],
                        read_d["mismatching_kmers"],
                        read_d["missing_kmers"],
                        offset))
                    offset += len(read_str)
                    n_reads += 1
                    if self.verbose: pbar.update(1)

            # Flag last line
            output_fp.write ("#\n")

        stderr_print ("[Eventalign_collapse] total reads: {} [{} reads/s]\n".format(n_reads, round (n_reads/(time()-t), 2)))

    #~~~~~~~~~~~~~~HELPER PRIVATE METHODS~~~~~~~~~~~~~~#
    def _init_kmer_dict (self, e, idx):
        """"""
        d = OrderedDict ()
        d["pos"] = e[idx["pos"]]
        d["kmer"] = e[idx["ref_kmer"]]
        d["n_events"] = 1
        d["NNNNN_events"] = 0
        d["mismatching_events"] = 0
        if e[idx["mod_kmer"]] == "NNNNN":
            d["NNNNN_events"] += 1
        elif e[idx["mod_kmer"]] != e[idx["ref_kmer"]]:
            d["mismatching_events"] += 1
        if "start" in idx:
            d["start"] = e[idx["start"]]
            d["end"] = e[idx["end"]]
        if "samples" in idx:
            d["sample_list"] = e[idx["samples"]].split(",")
        return d

    def _update_kmer_dict (self, d, e, idx):
        """"""
        d["n_events"] += 1
        if e[idx["mod_kmer"]] == "NNNNN":
            d["NNNNN_events"] += 1
        elif e[idx["mod_kmer"]] != e[idx["ref_kmer"]]:
            d["mismatching_events"] += 1
        if "start" in idx:
            d["start"] = e[idx["start"]]
        if "samples" in idx:
            d["sample_list"].extend (e[idx["samples"]].split(","))
        return d

    def _kmer_dict_to_str (self, d, idx):
        """"""
        # Write main fields
        s = "{}\t{}\t{}\t{}\t{}".format (d["pos"], d["kmer"], d["n_events"], d["NNNNN_events"], d["mismatching_events"])
        # Write extra fields
        if "start" in idx:
            s += "\t{}\t{}".format (d["start"], d["end"])
        if "samples" in idx:
            sample_array = np.array (d["sample_list"], dtype=np.float32)
            s += "\t{}\t{}\t{}".format (np.mean (sample_array), np.std (sample_array), len(sample_array))
            if self.write_samples:
                s += "\t{}".format(",".join(d["sample_list"]))
        s+="\n"
        return s

    def _get_field_idx (self, input_header):
        """"""
        # Get index of fields to fetch
        idx = OrderedDict()
        idx["pos"] = input_header.index ("position")
        idx["ref_kmer"] = input_header.index ("reference_kmer")
        idx["mod_kmer"] = input_header.index ("model_kmer")
        # Facultative field start and end index
        if "start_idx" in input_header and "end_idx" in input_header:
            idx["start"] = input_header.index ("start_idx")
            idx["end"] = input_header.index ("end_idx")
        # Facultative field samples
        if "samples" in input_header:
            idx["samples"] = input_header.index ("samples")
        return idx

    def _make_ouput_header (self, input_header):
        """"""
        # Add the main fields
        output_header_list = ["ref_pos", "ref_kmer", "n_events", "NNNNN_events", "mismatching_events"]
        # Facultative field start and end index
        if "start_idx" in input_header and "end_idx" in input_header:
            output_header_list.extend (["start_idx", "end_idx"])
        # Facultative field samples
        if "samples" in input_header:
            output_header_list.extend (["mean", "std", "n_signals"])
            if self.write_samples:
                output_header_list.append ("samples")
        # Convert output_header list to str
        output_header = "\t".join (output_header_list)
        return output_header
