#!/usr/bin/python3
import threading
import copy

import sqlite3
from DataObjects import DataObjectCollection, DataObject, detectorDataObject, partitionDataObject
class DataBaseWrapper:
    """Handler for the ECS Database"""
    connection = None

    def __init__(self):
        self.connection = sqlite3.connect("ECS_database.db")

    def close(self):
        """closes the database Connection"""
        self.connection.close()

    def getAllDetectors(self):
        """Get All Detectors in Detector Table; returns empty DataObjectCollection if there are now Detectors"""
        c = self.connection.cursor()
        try:
            c.execute("SELECT * FROM Detector")
            res = c.fetchall()
            return DataObjectCollection(res,detectorDataObject)
        except Exception as e:
            print("error getting detectors: %s" % str(e))
            return ECSCodes.error

    def getDetector(self,id):
        """get Detector with given id; returns ErrorCode if it does not exist"""
        c = self.connection.cursor()
        val = (id,)
        try:
            res = c.execute("SELECT * FROM Detector WHERE id = ?", val).fetchone()
            if not res:
                return ECSCodes.idUnknown
            return detectorDataObject(res)
        except Exception as e:
            print ("error getting detector: %s %s" % (str(id),str(e)))
            return ECSCodes.error

    def getAllUnmappedDetetectos(self):
        """gets all Detectors which are currently unmmaped"""
        c = self.connection.cursor()
        try:
            res = c.execute("SELECT * FROM Detector Where Detector.id not in (select DetectorId From Mapping)").fetchall()
            return DataObjectCollection(res,detectorDataObject)
        except Exception as e:
            print("error getting unmapped detectors: %s" % str(e))
            return ECSCodes.error

    def addDetector(self,dataObject):
        """add a Detector to Database;accepts json String or DataObject"""
        if not isinstance(dataObject,detectorDataObject):
            dataObject = detectorDataObject(json.loads(dataObject))
        c = self.connection.cursor()
        try:
            c.execute("INSERT INTO Detector VALUES (?,?,?,?,?)", dataObject.asArray())
            self.connection.commit()
            return ECSCodes.ok
        except Exception as e:
            print("error inserting values into Detector Table: %s" % str(e))
            self.connection.rollback()
            return ECSCodes.error


    def removeDetector(self,id):
        """delete a Detector from Database"""
        c = self.connection.cursor()
        val = (id,)
        try:
            c.execute("DELETE FROM Detector WHERE id = ?", val)
            self.connection.commit()
            return ECSCodes.ok
        except Exception as e:
            print("error removing values from Detector Table: %s" % str(e))
            return ECSCodes.error

    def getPartition(self,id):
        """Get Partition with given id from Database; returns None if it does not exist"""
        c = self.connection.cursor()
        val = (id,)
        try:
            res = c.execute("SELECT * FROM Partition WHERE id = ?", val).fetchone()
            if not res:
                return ECSCodes.idUnknown
            return partitionDataObject(res)
        except Exception as e:
            print("error getting partition %s: %s" % (str(id),str(e)))
            return ECSCodes.error

    def getPartitionForDetector(self,id):
        """gets the Partition of a Detector; returns DataObject or ErrorCode"""
        c = self.connection.cursor()
        val = (id,)
        try:
            res = c.execute("SELECT * FROM Partition WHERE Partition.id IN (SELECT PartitionId FROM (Mapping JOIN Partition ON Mapping.PartitionId = Partition.id) WHERE DetectorId = ?)", val).fetchone()
            if not res:
                return ECSCodes.idUnknown
            return partitionDataObject(res)
        except Exception as e:
            print("error getting partition for Detector %s: %s" % (str(id),str(e)))
            return ECSCodes.error

    def getAllPartitions(self):
        """Get All Detectors in Detector Table"""
        c = self.connection.cursor()
        try:
            c.execute("SELECT * FROM Partition")
            res = c.fetchall()
            return DataObjectCollection(res, partitionDataObject)
        except Exception as e:
            print("error getting all partitions: %s" % str(e))
            return ECSCodes.error

    def getDetectorsForPartition(self,pcaId):
        """get all Mapped Detectors for a given PCA Id"""
        c = self.connection.cursor()
        val = (pcaId,)
        try:
            c.execute("SELECT * From Detector WHERE Detector.id in (SELECT d.id FROM Detector d JOIN Mapping m ON d.id = m.DetectorId WHERE PartitionId=?)",val)
            res = c.fetchall()
            return DataObjectCollection(res, detectorDataObject)
        except Exception as e:
            print("error getting all detectors for Partition %s: %s" % str(pcaId), str(e))
            return ECSCodes.error

    def addPartition(self,dataObject):
        """create new Partition"""
        c = self.connection.cursor()
        data = dataObject.asArray()
        try:
            c.execute("INSERT INTO Partition VALUES (?,?,?,?,?,?,?,?)", data)
            self.connection.commit()
            return ECSCodes.ok
        except Exception as e:
            print("error inserting values into Partition Table: %s" % str(e))
            self.connection.rollback()
            return ECSCodes.error

    def removePartition(self,id):
        """delete a Partition with given id"""
        c = self.connection.cursor()
        val = (id,)
        try:
            c.execute("DELETE FROM Partition WHERE id = ?", val)
            #Free the Detectors
            c.execute("DELETE FROM Mapping WHERE PartitionId = ?", val)
            self.connection.commit()
            return ECSCodes.ok
        except Exception as e:
            self.connection.rollback()
            print("error removing values from Detector Table: %s" % str(e))
            return ECSCodes.error

    def mapDetectorToPCA(self,detId,pcaId):
        """map a Detector to a Partition"""
        c = self.connection.cursor()
        vals = (detId,pcaId)
        try:
            c.execute("INSERT INTO Mapping VALUES (?,?)", vals)
            self.connection.commit()
            return ECSCodes.ok
        except Exception as e:
            self.connection.rollback()
            print("error mapping %s to %s: %s" % (str(detId),str(pcaId),str(e)))
            return ECSCodes.error

    def remapDetector(self,detId,newPcaId,oldPcaID):
        c = self.connection.cursor()
        vals = (detId,newPcaId)
        try:
            c.execute("DELETE FROM Mapping WHERE DetectorId = ?", (detId))
            c.execute("INSERT INTO Mapping VALUES (?,?)", vals)
            self.connection.commit()
            return ECSCodes.ok
        except Exception as e:
            self.connection.rollback()
            print("error remapping %s from %s to %s: %s" % (str(detId),str(oldPcaID),str(newPcaId),str(e)))
            return ECSCodes.error

    def unmapDetectorFromPCA(self,detId):
        """unmap a Detector from a Partition"""
        c = self.connection.cursor()
        val = (detId,)
        try:
            c.execute("DELETE FROM Mapping WHERE DetectorId = ?", val)
            self.connection.commit()
            return ECSCodes.ok
        except Exception as e:
            self.connection.rollback()
            print("error unmapping %s: %s " % (str(detId),str(e)))
            return ECSCodes.error

    def usedPortsForAddress(self,address):
        """get all used Ports for an Ip-Address returns List of Ports or ErrorCode """
        c = self.connection.cursor()
        val = (address,)
        try:
            ports = []
            c.execute("SELECT Port,PingPort FROM Detector WHERE address=?",val)
            ret = c.fetchall()
            for row in ret:
                for val in row:
                    ports.append(val)
            c.execute("SELECT portPublish,portLog,portUpdates,portCurrentState,portCommand FROM Partition WHERE address=? ",val)
            ret = c.fetchall()
            for row in ret:
                for val in row:
                    ports.append(val)
            return ports
        except Exception as e:
            print("error getting Ports for Address %s: %s " % (address,str(e)))
            return ECSCodes.error

import zmq
import logging
import threading
import configparser
import ECSCodes
import struct
import json
from multiprocessing import Queue
import time
import ECS_tools
#import DataObjects

class ECS:
    """The Experiment Control System"""
    def __init__(self):
        self.database = DataBaseWrapper()
        self.detectors = self.database.getAllDetectors()
        self.partitions = ECS_tools.MapWrapper()
        partitions = self.database.getAllPartitions()
        for p in partitions:
            self.partitions[p.id] = p
        self.connectedPartitions = ECS_tools.MapWrapper()
        self.stateMap = ECS_tools.MapWrapper()

        self.commandSocketQueue = Queue()
        self.disconnectedPCAQueue = Queue()

        config = configparser.ConfigParser()
        config.read("init.cfg")
        conf = config["Default"]
        #todo configfile?
        self.receive_timeout = 2000
        self.pingIntervall = 2
        self.pingTimeout = 2000

        #subscribe to all PCAs
        self.ports = config["ZMQPorts"]
        self.zmqContext = zmq.Context()

        #socket for receiving requests from WebUI
        self.replySocket = self.zmqContext.socket(zmq.REP)
        #todo port out of config file
        self.replySocket.bind("tcp://*:%s" % "5000")

        #subscribe to all Partitions
        self.socketSubscription = self.zmqContext.socket(zmq.SUB)
        self.socketSubscription.setsockopt(zmq.SUBSCRIBE, b'')
        for p in self.partitions:
            address = p.address
            port = p.portPublish

            self.socketSubscription.connect("tcp://%s:%i" % (address,port))

        t = threading.Thread(name="updater", target=self.waitForUpdates)
        t.start()

        t = threading.Thread(name="requestHandler", target=self.waitForRequests)
        t.start()

        #get snapshots from PCAs
        for p in self.partitions:
            id = p.id
            address = p.address
            port = p.portCurrentState

            #if not self.getStateSnapshot(id,address,port):
            if not ECS_tools.getStateSnapshot(self.stateMap,address,port,timeout=self.receive_timeout,pcaid=id):
                self.handleDisconnection(id)
            else:
                self.connectedPartitions[id] = id
        self.reconnectorThread =  threading.Thread(name="reconnectorThread", target=self.reconnector)
        self.reconnectorThread.start()

        t = threading.Thread(name="commandHandler", target=self.commandSocketHandler)
        t.start()


    def commandSocketHandler(self):
        """send heartbeat/ping and commands on command socket"""
        nextPing = time.time() + self.pingIntervall
        while True:
            if not self.commandSocketQueue.empty():
                #sequential message processing might scale very badly if there a lot of pca especially if a pca has timeout
                id, command = self.commandSocketQueue.get()
                pca = self.partitions[id]
                socket = None
                socket = self.resetSocket(socket,pca.address,pca.portCommand,zmq.REQ)
                if command == ECSCodes.ping:
                    socket.setsockopt(zmq.RCVTIMEO, self.pingTimeout)
                else:
                    socket.setsockopt(zmq.RCVTIMEO, self.receive_timeout)

                #try to send message
                socket.send(command)
                r = None
                try:
                    r = socket.recv()
                except zmq.Again:
                    self.handleDisconnection(id)
                if r != ECSCodes.ok:
                    print("received error for sending command: %s " % str(command))
                socket.close()
                if not r:
                    #try to resend later ?
                    #self.commandSocketQueue.put(m)
                    continue
                if command != ECSCodes.ping:
                    #we've just send a message we don't need a ping
                    nextPing = time.time() + self.pingIntervall
            if time.time() > nextPing:
                #it is time for a ping
                for id in self.connectedPartitions:
                    self.commandSocketQueue.put((id,ECSCodes.ping))
                    nextPing = time.time() + self.pingIntervall

    def reconnector(self):
        """Thread which trys to reconnect to PCAs"""
        while True:#not self.disconnectedPCAQueue.empty()
            pca = self.disconnectedPCAQueue.get()
            if not ECS_tools.getStateSnapshot(self.stateMap,pca.address,pca.portCurrentState,pcaid=pca.id,timeout=self.receive_timeout):
                self.disconnectedPCAQueue.put(pca)
            else:
                self.connectedPartitions[pca.id] = pca.id

    def handleDisconnection(self,id):
        del self.connectedPartitions[id]
        self.disconnectedPCAQueue.put(self.partitions[id])

    def waitForRequests(self):
        #SQLite objects created in a thread can only be used in that same thread. So we need a second connection -_-
        db = DataBaseWrapper()
        while True:
            m = self.replySocket.recv_multipart()
            arg = None
            if len(m) == 2:
                code, arg = m
                arg = arg.decode()
            elif len(m) == 1:
                code = m[0]
            else:
                print ("received malformed request message: %s", str(m))
                continue

            def createPCA(arg):
                message = json.loads(arg)
                partition = partitionDataObject(json.loads(message["partition"]))
                detectors = message["detectors"]
                ret = db.addPartition(partition)
                if ret == ECSCodes.ok:
                    error = False
                    for detId in detectors:
                        ret = db.mapDetectorToPCA(detId,partition.id)
                        if ret == ECSCodes.error:
                            error = True
                    if error:
                        return ECSCodes.errorMapping
                    #connect to pca
                    self.partitions[partition.id] = partition
                    self.socketSubscription.connect("tcp://%s:%i" % (partition.address,partition.portPublish))
                    if not ECS_tools.getStateSnapshot(self.stateMap,partition.address,partition.portCurrentState,pcaid=partition.id,timeout=self.receive_timeout):
                        self.handleDisconnection(partition.id)
                    return ECSCodes.ok
                else:
                    return ECSCodes.errorCreatingPartition

            def mapDetectorsToPCA(arg):
                """map one or more Detectors to PCA"""
                detectors = json.loads(arg)
                for k,v in detectors.items():
                    ret = db.mapDetectorToPCA(k,v)
                    if ret == ECSCodes.error:
                        return ECSCodes.error
                return ECSCodes.ok
                #todo add Detectors to running system

            def remapDetector(arg):
                """moves a Detector between Partitions"""
                message = json.loads(arg)
                partitionId = message["partitionId"]
                detectorId = message["detectorId"]
                removed = False
                DCREconfigured = False
                dbChanged = False
                oldPartition = db.getPartitionForDetector(detectorId)
                newPartition = self.partitions[partitionId]
                detector = db.getDetector(detectorId)
                print("remapping")
                #change Database
                if db.remapDetector(detectorId,newPartition.id,oldPartition.id) == ECSCodes.error:
                    return ECSCodes.error
                print("db done")

                #remove from Old Partition
                requestSocket = self.zmqContext.socket(zmq.REQ)
                requestSocket.connect("tcp://%s:%s"  % (oldPartition.address,oldPartition.portCommand))
                requestSocket.setsockopt(zmq.RCVTIMEO, self.receive_timeout)
                requestSocket.send_multipart([ECSCodes.removeDetector,detectorId.encode()])
                try:
                    ret = requestSocket.recv()
                    requestSocket.close()
                except zmq.Again:
                    print ("timeout removing Detector from %s" % (oldPartition.id))
                    requestSocket.close()
                    return ECSCodes.error
                except Exception as e:
                    print ("error removing Detector from %s: %s " % (oldPartition.id,str(e)))
                    requestSocket.close()
                    return ECSCodes.error
                if ret != ECSCodes.ok:
                    print("%s returned error for removing Detector" % (oldPartition.id))
                    requestSocket.close()
                    return ECSCodes.error
                requestSocket.close()
                print("removed")

                #inform DetectorController
                requestSocket = self.zmqContext.socket(zmq.REQ)
                requestSocket.connect("tcp://%s:%s"  % (detector.address,detector.pingPort))
                requestSocket.setsockopt(zmq.RCVTIMEO, self.receive_timeout)
                requestSocket.send_multipart([ECSCodes.detectorChangePartition,newPartition.asJsonString().encode()])
                try:
                    ret = requestSocket.recv()
                    requestSocket.close()
                except zmq.Again:
                    print ("timeout changing Detector %s PCA" % (detector.id))
                    requestSocket.close()
                    return ECSCodes.error
                except Exception as e:
                    print ("error changing Detector %s PCA: %s " % (detector.id),str(e))
                    requestSocket.close()
                    return ECSCodes.error
                if ret != ECSCodes.ok:
                    print("%s returned error for changing PCA" % (detector.id))
                    requestSocket.close()
                    return ECSCodes.error
                requestSocket.close()
                print("detector informed")

                #add to new Partition
                requestSocket = self.zmqContext.socket(zmq.REQ)
                requestSocket.connect("tcp://%s:%s"  % (newPartition.address,newPartition.portCommand))
                requestSocket.setsockopt(zmq.RCVTIMEO, self.receive_timeout)
                requestSocket.send_multipart([ECSCodes.addDetector,detector.asJsonString().encode()])
                try:
                    ret = requestSocket.recv()
                    requestSocket.close()
                except zmq.Again:
                    print ("timeout adding Detector to %s" % (newPartition.id))
                    requestSocket.close()
                    return ECSCodes.error
                except Exception as e:
                    print ("error adding Detector to %s: %s " % (newPartition.id,str(e)))
                    requestSocket.close()
                    return ECSCodes.error
                if ret != ECSCodes.ok:
                    print("%s returned error for removing Detector" % (newPartition.id))
                    requestSocket.close()
                    return ECSCodes.error
                requestSocket.close()
                print("detector added")
                return ECSCodes.ok

            def switcher(code,arg=None):
                #functions for codes
                dbFunctionDictionary = {
                    ECSCodes.pcaAsksForConfig: db.getPartition,
                    ECSCodes.detectorAsksForPCA: db.getPartitionForDetector,
                    ECSCodes.getDetectorForId: db.getDetector,
                    ECSCodes.pcaAsksForDetectorList: db.getDetectorsForPartition,
                    ECSCodes.getPartitionForId: db.getPartition,
                    ECSCodes.getAllPCAs: db.getAllPartitions,
                    ECSCodes.getUnmappedDetectors: db.getAllUnmappedDetetectos,
                    ECSCodes.createPartition: createPCA,
                    ECSCodes.createDetector: db.addDetector,
                    ECSCodes.mapDetectorsToPCA: mapDetectorsToPCA,
                    ECSCodes.detectorChangePartition: remapDetector,
                }
                #returns function for Code or None if the received code is unknown
                f = dbFunctionDictionary.get(code,None)
                if not f:
                    self.replySocket.send(ECSCodes.unknownCommand)
                    return
                if arg:
                    ret = f(arg)
                else:
                    ret = f()
                #is result a Dataobject?
                if isinstance(ret,DataObject) or isinstance(ret,DataObjectCollection):
                    #encode Dataobject
                    ret = ret.asJsonString().encode()
                    self.replySocket.send(ret)
                else:
                    #it's just a returncode
                    self.replySocket.send(ret)
            if arg:
                switcher(code,arg)
            else:
                switcher(code)

    def resetSocket(self,socket,address,port,type):
        """resets a socket with address and zmq Type; if socket is None a new socket will be created"""
        if socket != None:
            socket.close()
        socket = self.zmqContext.socket(type)
        socket.connect("tcp://%s:%s" % (address,port))
        if type == zmq.REQ:
            socket.setsockopt(zmq.RCVTIMEO, self.receive_timeout)
        socket.setsockopt(zmq.LINGER,0)
        return socket

    def waitForUpdates(self):
        #watch subscription for further updates
        while True:
            m = self.socketSubscription.recv_multipart()
            if len(m) != 3:
                print (m)
            else:
                id, sequence, state = m
                id = id.decode()
                if state == ECSCodes.reset:
                    #delete PCA and Detectors associated with PCA From Map
                    db = DataBaseWrapper()
                    dets = db.getDetectorsForPartition(id).asDictionary()
                    arg = list(dets.keys())
                    arg.append(id)
                    self.stateMap.delMany(arg)
                    print("reset %s" % id)
                    continue
                if state == ECSCodes.removed:
                    del self.stateMap[id]
                    continue
                state = state.decode()
            sequence = ECS_tools.intFromBytes(sequence)
            print("received update",id, sequence, state)
            if id in self.stateMap:
                #only update if the current status sequence is smaller
                if self.stateMap[id][0] < sequence:
                    self.stateMap[id] = (sequence, state)
            else:
                self.stateMap[id] = (sequence, state)


if __name__ == "__main__":
    test = ECS()
    input()
